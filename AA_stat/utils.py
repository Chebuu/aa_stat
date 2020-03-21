import matplotlib
matplotlib.use('Agg')

import pylab as plt
import ast
import os
import logging
from scipy.optimize import curve_fit
from scipy.signal import argrelextrema, savgol_filter
import pandas as pd
import numpy as  np
import warnings
from collections import defaultdict
import re
import seaborn as sb
try:
    from configparser import ConfigParser
except ImportError:
    from ConfigParser import ConfigParser
import math
import multiprocessing as mp
from pyteomics import parser, pepxml, mgf, mzml

logger = logging.getLogger(__name__)
logging.getLogger('matplotlib.font_manager').disabled = True
MASS_FORMAT = '{:+.4f}'
AA_STAT_PARAMS_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'example.cfg')
FIT_BATCH = 900


cc = ["#FF6600",
      "#FFCC00",
      "#88AA00",
      "#006688",
      "#5FBCD3",
      "#7137C8",
      ]
sb.set_style('white')
colors = sb.color_palette(palette=cc)

def mass_format(mass):
    return MASS_FORMAT.format(mass)


def make_0mc_peptides(pep_list, rule):
    """
    In silico cleaves all peptides with a given rule.

    Parameters
    ----------
    pep_list : Iterable
        An iterable of peptides
    rule : str or compiled regex.
        Cleavage rule in pyteomics format.

    Returns
    -------
    Set of fully cleaved peptides.

    """
    out_set = set()
    for i in pep_list:
        out_set.update(parser.cleave(i, rule))
    return out_set


def read_pepxml(fname, params_dict):
    return pepxml.DataFrame(fname, read_schema=False)


def read_csv(fname, params_dict):
    """
    Reads csv file.

    Paramenters
    -----------
    fname : str
        Path to file name.
    params_dict : dict
        Dict with paramenters for parsing csv file.
            `csv_delimiter`, `proteins_column`, `proteins_delimiter`
    Returns
    -------
    A DataFrame of csv file.

    """
    df = pd.read_csv(fname, sep=params_dict['csv_delimiter'])
    protein = params_dict['proteins_column']
    if (df[protein].str[0] == '[').all() and (df[protein].str[-1] == ']').all():
        df[protein] = df[protein].apply(ast.literal_eval)
    else:
        df[protein] = df[protein].str.split(params_dict['proteins_delimeter'])
    return df


def read_input(args, params_dict):
    """
    Reads open search output, assembles all data in one DataFrame.

    """
    dfs = []
    data = pd.DataFrame()
    window = 0.3
    zero_bin = 0
    logger.info('Reading input files...')
    readers = {
        'pepxml': read_pepxml,
        'csv': read_csv,
    }
    shifts = params_dict['mass_shifts_column']
    for ftype, reader in readers.items():
        filenames = getattr(args, ftype)
        logger.debug('Filenames: %s', filenames)
        if filenames:
            for filename in filenames:
                logger.info('Reading %s', filename)
                df = reader(filename, params_dict)
                hist_0 = np.histogram(df.loc[abs(df[shifts] - zero_bin) < window/2, shifts],
                    bins=10000)
                # logger.debug('hist_0: %s', hist_0)
                hist_y = hist_0[0]
                hist_x = 0.5 * (hist_0[1][:-1] + hist_0[1][1:])
                popt, perr = gauss_fitting(max(hist_y), hist_x, hist_y)
                logger.info('Systematic shift for file is %.4f Da', popt[1])
                df[shifts] -= popt[1]
                df['file'] = os.path.split(filename)[-1].split('.')[0]  # correct this
                dfs.append(df)
            break
    logger.info('Starting analysis...')
    data = pd.concat(dfs, axis=0)
    data.index = range(len(data))
    data['is_decoy'] = data[params_dict['proteins_column']].apply(
        lambda s: all(x.startswith(params_dict['decoy_prefix']) for x in s))

    data['bin'] = np.digitize(data[shifts], params_dict['bins'])
    return data


def get_unimod_url(mass_shift):
    return ('http://www.unimod.org/modifications_list.php'
        '?a=search&value=1&SearchFor={:.0f}.&'
        'SearchOption=Starts+with+...&SearchField=mono_mass'.format(mass_shift))


def smooth(y, window_size=15, power=5):
    """
    Smoothes function.
    Paramenters
    -----------
    y : array-like
        function to smooth.
    window_size : int
        Smothing window.
    power : int
        Power of smothing function.

    Returns
    -------
    Smoothed function

    """
    y_smooth = savgol_filter(y, window_size, power)
    return y_smooth


def gauss(x, a,  x0, sigma):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return a/sigma/np.sqrt(2*np.pi) * np.exp(-(x - x0) * (x - x0) / (2 * sigma ** 2))


def gauss_fitting(center_y, x, y):
    """
    Fits with Gauss function
    `center_y` - starting point for `a` parameter of gauss
    `x` numpy array of mass shifts
    `y` numpy array of number of psms in this mass shifts

    """
    mean = sum(x*y) / sum(y)
    sigma = np.sqrt(sum(y * (x - mean) ** 2) / sum(y))
    a = center_y * sigma * np.sqrt(2*np.pi)
    try:
        popt, pcov = curve_fit(gauss, x, y, p0=(a, mean, sigma))
        perr = np.sqrt(np.diag(pcov))
        return popt, perr
    except (RuntimeError, TypeError):
        return None, None


def fit_worker(args):
    return fit_batch_worker(*args)


def fit_batch_worker(out_path, batch_size, xs, ys, half_window, height_error, sigma_error):
    shape = int(math.ceil(np.sqrt(batch_size)))
    figsize = (shape * 3, shape * 4)
    plt.figure(figsize=figsize)
    plt.tight_layout()
    logger.debug('Created a figure with size %s', figsize)
    poptpvar = []
    for i in range(batch_size):
        center = i * (2 * half_window + 1) + half_window
        x = xs[center - half_window : center + half_window + 1]
        y = ys[center - half_window : center + half_window + 1]
        popt, perr = gauss_fitting(ys[center], x, y)
        plt.subplot(shape, shape, i+1)
        if popt is None:
            label = 'NO FIT'
        else:
            if (x[0] <= popt[1] and popt[1] <= x[-1] and perr[0]/popt[0] < height_error
                and perr[2]/popt[2] < sigma_error):
                label = 'PASSED'
                poptpvar.append(np.concatenate([popt, perr]))
                plt.vlines(popt[1] - 3 * popt[2], 0, ys[center], label='3sigma interval')
                plt.vlines(popt[1] + 3 * popt[2], 0, ys[center])
            else:
                label='FAILED'
        plt.plot(x, y, 'b+:', label=label)
        if label != 'NO FIT':
            plt.scatter(x, gauss(x, *popt),
                        label='Gaussian fit\n $\sigma$ = ' + "{0:.4f}".format(popt[2]) )

        plt.legend()
        plt.title("{0:.3f}".format(xs[center]))
        plt.grid(True)

    logger.debug('Fit done. Saving %s...', out_path)
    plt.savefig(out_path)
    plt.close()
    return poptpvar


def fit_peaks(data, args, params_dict):
    """
    Finds Gauss-like peaks in mass shift histogram.

    Parameters
    ----------
    data : DataFRame
        A DF with all (non-filtered) results of open search.
    args: argsparse
    params_dict : dict
        Parameters dict.
    """
    logger.info('Performing Gaussian fit...')

    half_window = int(params_dict['window']/2) + 1
    hist = np.histogram(data[params_dict['mass_shifts_column']], bins=params_dict['bins'])
    hist_y = smooth(hist[0], window_size=params_dict['window'], power=5)
    hist_x = 0.5 * (hist[1][:-1] + hist[1][1:])
    loc_max_candidates_ind = argrelextrema(hist_y, np.greater_equal)[0]
    # smoothing and finding local maxima
    min_height = 2 * np.median(hist[0][hist[0] > 1])
    # minimum bin height expected to be peak approximate noise level as median of all non-negative
    loc_max_candidates_ind = loc_max_candidates_ind[hist_y[loc_max_candidates_ind] >= min_height]


    height_error = params_dict['max_deviation_height']
    sigma_error = params_dict['max_deviation_sigma']
    logger.debug('Candidates for fit: %s', len(loc_max_candidates_ind))
    nproc = int(math.ceil(len(loc_max_candidates_ind) / FIT_BATCH))
    if nproc > 1:
        arguments = []
        logger.debug('Splitting the fit into %s batches...', nproc)
        n = min(nproc, mp.cpu_count())
        logger.debug('Creating a pool of %s processes.', n)
        pool = mp.Pool(n)
        for proc in range(nproc):
            xlist = [hist_x[center - half_window: center + half_window + 1]
                for center in loc_max_candidates_ind[proc*FIT_BATCH:(proc+1)*FIT_BATCH]]
            xs = np.concatenate(xlist)
            ylist = [hist[0][center - half_window: center + half_window + 1]
                for center in loc_max_candidates_ind[proc*FIT_BATCH:(proc+1)*FIT_BATCH]]
            ys = np.concatenate(ylist)
            out = os.path.join(args.dir, 'gauss_fit_{}.pdf'.format(proc+1))
            arguments.append((out, len(xlist), xs, ys, half_window, height_error, sigma_error))
        res = pool.map_async(fit_worker, arguments)
        poptpvar_list = res.get()
        # logger.debug(poptpvar_list)
        pool.close()
        pool.join()
        logger.debug('Workers done.')
        poptpvar = [p for r in poptpvar_list for p in r]
    else:
        xs = np.concatenate([hist_x[center - half_window: center + half_window + 1]
                for center in loc_max_candidates_ind])
        ys = np.concatenate([hist[0][center - half_window: center + half_window + 1]
                for center in loc_max_candidates_ind])
        poptpvar = fit_batch_worker(os.path.join(args.dir, 'gauss_fit.pdf'),
            len(loc_max_candidates_ind), xs, ys, half_window, height_error, sigma_error)

    logger.debug('Returning from fit_peaks.')
    return hist, np.array(poptpvar)


def read_mgf(file_path):
    return mgf.IndexedMGF(file_path)


def read_mzml(file_path): # write this
    return mzml.PreIndexedMzML(file_path)


def read_spectra(args):

    readers = {
        'mgf': read_mgf,
        'mzML': read_mzml,
    }
    out_dict = {}
    for ftype, reader in readers.items():
        filenames = getattr(args, ftype)
        if filenames:
            for filename in filenames:
                name = os.path.split(filename)[-1].split('.')[0] #write it in a proper way
                out_dict[name] = reader(filename)
    return out_dict


def read_config_file(fname):
    params = ConfigParser(delimiters=('=', ':'),
                          comment_prefixes=('#'),
                          inline_comment_prefixes=('#'))

    params.read(AA_STAT_PARAMS_DEFAULT)
    if fname:
        if not os.path.isfile(fname):
            logger.error('Configuration file not found: %s', fname)
        else:
            params.read(fname)
    else:
        logger.info('Using default parameters for AA_stat.')
    return params


def get_parameters(params):
    """
    Reads paramenters from cfg file to one dict.
    Returns dict.
    """
    parameters_dict = defaultdict()
    #data
    parameters_dict['decoy_prefix'] = params.get('data', 'decoy prefix')
    parameters_dict['FDR'] = params.getfloat('data', 'FDR')
    parameters_dict['labels'] = params.get('data', 'labels').strip().split()
    parameters_dict['rule'] = params.get('data', 'cleavage rule')
    # csv input
    parameters_dict['csv_delimiter'] = params.get('csv input', 'delimiter')
    parameters_dict['proteins_delimeter'] = params.get('csv input', 'proteins delimiter')
    parameters_dict['proteins_column'] = params.get('csv input', 'proteins column')
    parameters_dict['peptides_column'] = params.get('csv input', 'peptides column')
    parameters_dict['mass_shifts_column'] = params.get('csv input', 'mass shift column')
    #general
    parameters_dict['bin_width'] = params.getfloat('general', 'width of bin in histogram')
    parameters_dict['so_range'] = tuple(float(x) for x in params.get('general', 'open search range').split(','))
    parameters_dict['area_threshold'] = params.getint('general', 'threshold for bins') # area_thresh
    parameters_dict['walking_window'] = params.getfloat('general', 'shifting window') #shifting_window
    parameters_dict['FDR_correction'] = params.getboolean('general', 'FDR correction') #corrction

    parameters_dict['specific_mass_shift_flag'] = params.getboolean('general', 'use specific mass shift window') #spec_window_flag
    parameters_dict['specific_window'] = [float(x) for x in params.get('general', 'specific mass shift window').split(',')] #spec_window

    parameters_dict['figsize'] = tuple(float(x) for x in params.get('general', 'figure size in inches').split(','))
    #fit
#    parameters_dict['shift_error'] = params.getint('fit', 'shift error')
#    parameters_dict['max_deviation_x'] = params.getfloat('fit', 'standard deviation threshold for center of peak')
    parameters_dict['max_deviation_sigma'] = params.getfloat('fit', 'standard deviation threshold for sigma')
    parameters_dict['max_deviation_height'] = params.getfloat('fit', 'standard deviation threshold for height')
    #localization
    parameters_dict['spectrum_column'] = params.get('localization', 'spectrum column')
    parameters_dict['charge_column'] = params.get('localization', 'charge column')
    return parameters_dict


def set_additional_params(params_dict):
    """
    Updates dict with new paramenters.
    Returns dict.
    """
    if params_dict['specific_mass_shift_flag']:
        logger.info('Custom bin: %s', params_dict['specific_window'])
        params_dict['so_range'] = params_dict['specific_window'][:]

    elif params_dict['so_range'][1] - params_dict['so_range'][0] > params_dict['walking_window']:
        window = params_dict['walking_window'] / params_dict['bin_width']

    else:
        window = (params_dict['so_range'][1] - params_dict['so_range']) / params_dict['bin_width']
    if int(window) % 2 == 0:
        params_dict['window'] = int(window) + 1
    else:
        params_dict['window'] = int(window)  #should be odd
    params_dict['bins'] = np.arange(params_dict['so_range'][0],
        params_dict['so_range'][1] + params_dict['bin_width'], params_dict['bin_width'])



_Mkstyle = matplotlib.markers.MarkerStyle
_marker_styles = [_Mkstyle('o', fillstyle='full'), (_Mkstyle('o', fillstyle='left'), _Mkstyle('o', fillstyle='right')),
    (_Mkstyle('o', fillstyle='top'), _Mkstyle('o', fillstyle='bottom')), (_Mkstyle(8), _Mkstyle(9)),
    (_Mkstyle('v'), _Mkstyle('^')), (_Mkstyle('|'), _Mkstyle('_')), (_Mkstyle('+'), _Mkstyle('x'))]

def plot_figure(ms_label, ms_counts, left, right, params_dict, save_directory, localizations=None, sumof=None):
    """
    Plots amino acid spatistics.

    Parameters
    ----------
    ms_label : str
        Mass shift in string format.
    ms_counts : int
        Number of peptides in a mass shift.
    left : list
        Amino acid statistics data [[values], [errors]]
    right : list
        Amino acid frequences in peptides
     params_dict : dict
        Parameters dict.
    save_directory: str
        Saving directory.
    localizations : Counter
         Localization counter using  ms/ms level.
    sumof : List
        List of str tuples for constituent mass shifts.

    """
    b = 0.1 # shift in bar plots
    width = 0.2 # for bar plots
    labels = params_dict['labels']
    labeltext = ms_label + ' Da mass shift,\n' + str(ms_counts) + ' peptides'
    x = np.arange(len(labels))
    distributions = left[0]
    errors = left[1]

    fig, ax_left = plt.subplots()
    fig.set_size_inches(params_dict['figsize'])

    ax_left.bar(x-b, distributions.loc[labels],
            yerr=errors.loc[labels], width=width, color=colors[2], linewidth=0)

    ax_left.set_ylabel('Relative AA abundance', color=colors[2])
    ax_left.set_xticks(x)
    ax_left.set_xticklabels(labels)
    ax_left.hlines(1, -1, x[-1] + 1, linestyles='dashed', color=colors[3])
    ax_right = ax_left.twinx()

    ax_right.bar(x+b, right, width=width, linewidth=0, color=colors[0])

    ax_right.set_ylim(0, 125)
    ax_right.set_yticks(np.arange(0, 120, 20))
    ax_right.set_ylabel('Peptides with AA, %', color=colors[0])

    ax_left.spines['left'].set_color(colors[2])
    ax_right.spines['left'].set_color(colors[2])

    ax_left.spines['right'].set_color(colors[0])
    ax_right.spines['right'].set_color(colors[0])
    ax_left.tick_params('y', colors=colors[2])
    ax_right.tick_params('y', colors=colors[0])

    pright = matplotlib.lines.Line2D([], [], marker=None, label=labeltext, alpha=0)


    ax_left.set_xlim(-1, x[-1] + 1)
    ax_left.set_ylim(0, distributions.loc[labels].max() * 1.4)

    logger.debug('Localizations for %s figure: %s', ms_label, localizations)
    if localizations:
        ax3 = ax_left.twinx()
        ax3.spines['right'].set_position(('axes', 1.1))
        ax3.set_frame_on(True)
        ax3.patch.set_visible(False)
        ax3.set_ylabel('Localization count', color=colors[3])
        for sp in ax3.spines.values():
            sp.set_visible(False)
        ax3.spines['right'].set_visible(True)
        ax3.spines['right'].set_color(colors[3])
        ax3.tick_params('y', colors=colors[3])
        # plot simple modifications (not sum) with the first style,
        # then parts of sum as second and third style
        values = [localizations.get(key+ '_' + ms_label) for key in labels]
        label_prefix = 'Location of '
        ax3.scatter(x, values, marker=_marker_styles[0], color=colors[3], label=label_prefix+ms_label)
        if isinstance(sumof, list):
            for pair, styles in zip(sumof, _marker_styles[1:]):
                values_1 = [localizations.get(key + '_' + pair[0]) for key in labels]
                ax3.scatter(x, values_1, marker=styles[0], color=colors[3], label=label_prefix+pair[0])
                if pair[0] != pair[1]:
                    values_2 = [localizations.get(key + '_' + pair[1]) for key in labels]
                    if values_2:
                        ax3.scatter(x, values_2, marker=styles[1], color=colors[3], label=label_prefix+pair[1])
                else:
                    values_2 = []
        else:
            values_1 = values_2 = []
        terms = {key for key in localizations if key[1:6] == '-term'}
        logger.debug('Found terminal localizations: %s', terms)
        for t in terms:
            label = '{} at {}: {}'.format(*reversed(t.split('_')), localizations[t])
            p = ax3.plot([], [], label=label)[0]
            p.set_visible(False)
        pright.set_label(pright.get_label() + '\nNot localized: {}'.format(localizations.get('non-localized', 0)))
        ax3.set_ylim(0, 1.4 * max(x for x in values + values_1 + values_2 if x is not None))
        ax3.legend(loc='upper left', ncol=2)
    ax_right.legend(handles=[pright], loc='upper right', edgecolor='dimgrey', fancybox=True, handlelength=0)
    fig.tight_layout()
    fig.savefig(os.path.join(save_directory, ms_label + '.png'), dpi=500)
    fig.savefig(os.path.join(save_directory, ms_label + '.svg'))
    plt.close()


def summarizing_hist(table, save_directory):
    ax = table.sort_values('mass shift').plot(
        y='# peptides in bin', kind='bar', color=colors[2], figsize=(len(table), 5))
    ax.set_title('Peptides in mass shifts', fontsize=12) #PSMs
    ax.set_xlabel('Mass shift', fontsize=10)
    ax.set_ylabel('Number of peptides')
    ax.set_xticklabels(table.sort_values('mass shift')['mass shift'].apply(lambda x: round(x, 2)))

    total = sum(i.get_height() for i in ax.patches)
    max_height = 0
    for i in ax.patches:
        current_height = i.get_height()
        if current_height > max_height:
            max_height = current_height
        ax.text(i.get_x()-.03, current_height + 40,
            '{:>6.2%}'.format(i.get_height() / total), fontsize=10, color='dimgrey')

    plt.ylim(0, max_height * 1.2)
    plt.tight_layout()
    plt.savefig(os.path.join(save_directory, 'summary.png')) #dpi=500
    plt.savefig(os.path.join(save_directory, 'summary.svg'))


def render_html_report(table_, params_dict, save_directory):
    table = table_.copy()
    labels = params_dict['labels']
    report_template = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'report.template')
    with open(report_template) as f:
        report = f.read()
    with pd.option_context('display.max_colwidth', 250):
        columns = list(table.columns)
        mslabel = '<a id="binh" href="#">mass shift</a>'
        columns[0] = mslabel
        table.columns = columns
        table_html = table.style.hide_index().applymap(
            lambda val: 'background-color: yellow' if val > 1.5 else '', subset=labels
            ).set_precision(3).set_table_styles([
            {'selector': 'tr:hover', 'props': [('background-color', 'lightyellow')]},
            {'selector': 'td, th', 'props': [('text-align', 'center')]},
            {'selector': 'td, th', 'props': [('border', '1px solid black')]}]
            ).format({'Unimod': '<a href="{}">search</a>'.format,
                mslabel: '<a href="#">{}</a>'.format(MASS_FORMAT).format,
                '# peptides in bin': '<a href="#">{}</a>'.format}
            ).bar(subset='# peptides in bin', color=cc[2]).render() #PSMs

    peptide_tables = []
    for ms in table.index:
        fname = os.path.join(save_directory, ms+'.csv')
        if os.path.isfile(fname):
            df = pd.read_csv(fname, sep='\t')
            if 'localization score' in df:
                out = df.sort_values(['localization score'], ascending=False)
            else:
                out = df
            peptide_tables.append(out.to_html(table_id='peptides_'+ms, classes=('peptide_table',), index=False, escape=False,
                formatters={'top isoform': lambda form: re.sub(r'([A-Z]\[[+-]?[0-9]+\])', r'<span class="loc">\1</span>', form),
                'localization score': lambda v: '' if pd.isna(v) else '{:.2f}'.format(v)},
                ))
        else:
            logger.debug('File not found: %s', fname)


    report = report.replace(r'%%%', table_html).replace(r'&&&', '\n'.join(peptide_tables))
    with open(os.path.join(save_directory, 'report.html'), 'w') as f:
        f.write(report)


def format_isoform(seq, ms):
    return re.sub(r'([a-z])([A-Z])', lambda m: '{}[{:+.0f}]'.format(m.group(2), float(ms[m.group(1)])), seq)


def table_path(dir, ms):
    return os.path.join(dir, ms + '.csv')


def save_df(ms, df, save_directory, peptide, spectrum):
    with open(table_path(save_directory, ms), 'w') as out:
            df[[peptide, spectrum]].to_csv(out, index=False, sep='\t')


def save_peptides(data, save_directory, params_dict):
    peptide = params_dict['peptides_column']
    spectrum = params_dict['spectrum_column']
    for ms_label, (ms, df) in data.items():
        save_df(ms_label, df, save_directory, peptide, spectrum)


def get_fix_modifications(pepxml_file):
    out = {}
    p = pepxml.PepXML(pepxml_file, use_index=False)
    mod_list = list(p.iterfind('aminoacid_modification'))
    for m in mod_list:
        out[m['aminoacid']] = m['mass']
    return out
