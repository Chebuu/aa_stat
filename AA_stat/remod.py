# -*- coding: utf-8 -*-
"""
Created on Thu Oct 24 11:44:50 2019

@author: Julia
"""

from __future__ import print_function, division
import matplotlib
matplotlib.use('Agg')
import pandas as pd
import numpy as  np
import pylab as plt
import os
import argparse
import ast
import seaborn as sb
from collections import defaultdict
from scipy.signal import argrelextrema, savgol_filter
from scipy.stats import ttest_ind
from scipy.optimize import curve_fit
import logging
import warnings
from pyteomics import parser, pepxml, mgf, mzml, mass
from pyteomics import electrochem as ec
from scipy.spatial import cKDTree
import numpy as np
from math import factorial
from copy import copy
try:
    from pyteomics import cmass
except ImportError:
    cmass = mass

#    import customparser as cparser

def get_spectra_from_mgf(file_dir, spectrum, suffix, tolerance=0.01):
    """
    Retrive MSMS `spectra` from `file_name` file.
    Returns  spectrum_idict {int_mass:intensity}
    """
    file_name = '.'.join([spectrum.split('.')[0], suffix])
    mgf_file = mgf.read(os.path.join(file_dir, file_name))
    mgf_file.get_spectrum(spectrum)
    experimantal_spectrum =  mgf_file.get_spectrum(spectrum)
    mz = np.array(experimantal_spectrum['m/z array'])/ tolerance 
    return dict(zip(mz.round(0).astype('int64'), experimantal_spectrum['intensity array']))

def get_theor_spectrum(peptide, acc_frag, types=('b', 'y'), maxcharge=None, reshape=False, **kwargs):
    peaks = {}
    theoretical_set = defaultdict(set)#set()
    # theoretical_set = set()
    pl = len(peptide) - 1
    if not maxcharge:
        maxcharge = 1 + int(ec.charge(peptide, pH=2))
    for charge in range(1, maxcharge + 1):
        for ion_type in types:
            nterminal = ion_type[0] in 'abc'
            if nterminal:
                maxpart = peptide[:-1]
                maxmass = cmass.fast_mass(maxpart, ion_type=ion_type, charge=charge, **kwargs)
                marr = np.zeros((pl, ), dtype=float)
                marr[0] = maxmass
                for i in range(1, pl):
                    marr[i] = marr[i-1] - mass.fast_mass2([maxpart[-i]])/charge ### recalculate
            else:
                maxpart = peptide[1:]
                maxmass = cmass.fast_mass(maxpart, ion_type=ion_type, charge=charge, **kwargs)
                marr = np.zeros((pl, ), dtype=float)
                marr[pl-1] = maxmass
                for i in range(pl-2, -1, -1):
                    marr[i] = marr[i+1] - mass.fast_mass2([maxpart[-(i+2)]])/charge ### recalculate

            tmp = marr / acc_frag
            tmp = tmp.astype(int)
            theoretical_set[ion_type].update(tmp)
            if not reshape:
                marr.sort()
            else:
                n = marr.size
                marr = marr.reshape((n, 1))
            peaks[ion_type, charge] = marr
    return peaks, theoretical_set


def RNHS_fast(spectrum_idict, theoretical_set, min_matched):
    isum = 0
    matched_approx_b, matched_approx_y = 0, 0
    for ion in theoretical_set['b']:
        if ion in spectrum_idict:
            matched_approx_b += 1
            isum += spectrum_idict[ion]

    for ion in theoretical_set['y']:
        if ion in spectrum_idict:
            matched_approx_y += 1
            isum += spectrum_idict[ion]
    matched_approx = matched_approx_b + matched_approx_y
    if matched_approx >= min_matched:
        return matched_approx, factorial(matched_approx_b) * factorial(matched_approx_y) * isum
    else:
        return 0, 0
    
    
def peptide_isoforms(sequence, mass_shift, aa_stat_candidates=None):
    """
    Forms list of modified amino acid candidates.
    Return list of amino acids.
    
    """
    pass

def retrive_candidates_from_unimod(mass_shift, tolerance):
    
    """
    Find modificationsfor `mass_shift` in Unimod.org database with a given `tolerance`.
    Returns dict. {'modification name': [list of amino acids]}
    
    """
    
    pass