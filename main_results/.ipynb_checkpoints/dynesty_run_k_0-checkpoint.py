""""
Script to run PE run using dynesty sampler for TaylorF2 model 
keeping ra, dec and polarization fixed to their MAP values
Author: Lalit Pathak(lalit.pathak@iitgn.ac.in)

""""

import dynesty
import numpy as np

import h5py
import time
import multiprocessing as mp

import pycbc
from pycbc.catalog import Merger
from pycbc.frame import read_frame
from pycbc.detector import Detector
from pycbc.pnutils import f_SchwarzISCO
from pycbc.psd import interpolate, welch
from pycbc.filter import highpass, matched_filter
from pycbc.types import FrequencySeries,TimeSeries
from pycbc.psd import interpolate, inverse_spectrum_truncation
from pycbc.conversions import mass1_from_mchirp_q, mass2_from_mchirp_q
from pycbc.conversions import mass1_from_mchirp_eta, mass2_from_mchirp_eta
from pycbc.inference.models import MarginalizedPhaseGaussianNoise, GaussianNoise
from pycbc.waveform.generator import (FDomainDetFrameGenerator, FDomainCBCGenerator)

merger = Merger("GW170817")
ifos = ['L1', 'H1', 'V1'] # defining a list of interferometers
fLow = 20 # seismic cutoff frequency
whichfrac = float(input('fraction of fISCO: ')) # fraction of fISCO corresponding to M_map value (in Hz)
M_map = 2.76 # MAP total-mass value taken from bilby samples 
fHigh = whichfrac*f_SchwarzISCO(M_map) # high cutoff frequency

strain, stilde = {}, {}
low_frequency_cutoff = {}
high_frequency_cutoff = {}

for ifo in ifos:
    
    low_frequency_cutoff[ifo] = fLow
    high_frequency_cutoff[ifo] = fHigh
    
#-- reading GW170817 data ---
#-- We use 360 seconds open archival GW170817 data(containing the trigger)from GWOSC ---
#-- Using PyCBC utilities to perform some cleaning jobs on the raw data ---

for ifo in ifos:
    
    ts = read_frame("{}-{}_LOSC_CLN_4_V1-1187007040-2048.gwf".format(ifo[0], ifo),
                    '{}:LOSC-STRAIN'.format(ifo),
                   start_time=merger.time - 342,   
                   end_time=merger.time + 30,     
                   check_integrity=False)
    
    # Read the detector data and remove low frequency content
    strain[ifo] = highpass(ts, 18, filter_order=4)
    
    # Remove time corrupted by the high pass filter
    strain[ifo] = strain[ifo].crop(6,6)

    # Also create a frequency domain version of the data
    stilde[ifo] = strain[ifo].to_frequencyseries()

#-- calculating psds ---

psds = {}

for ifo in ifos:
    # Calculate a psd from the data. We'll use 2s segments in a median - welch style estimate
    # We then interpolate the PSD to the desired frequency step. 
    psds[ifo] = interpolate(strain[ifo].psd(2), stilde[ifo].delta_f)

    # We explicitly control how much data will be corrupted by overwhitening the data later on
    # In this case we choose 2 seconds.
    psds[ifo] = inverse_spectrum_truncation(psds[ifo], int(2 * strain[ifo].sample_rate),
                                    low_frequency_cutoff=low_frequency_cutoff[ifo], trunc_method='hann')

#-- link: https://dcc.ligo.org/LIGO-P1800370/public ---
#-- paper link: https://arxiv.org/pdf/1805.11579.pdf ---
#-- median values taken from the GW170817_GWTC-1.hdf5 files (low-spin) ---
approximant = 'TaylorF2'
polarization = 0 #radian
ra = 3.44616 #radian
dec = -0.408084 #radian 

#-- setting fixed parameters and factors ---
static_params = {'approximant': approximant, 'f_lower': fLow, 'f_higher': fHigh, 'ra': ra, 'dec': dec,
                        'polarization': polarization}

variable_params = ['mass1', 'mass2', 'spin1z', 'spin2z', 'inclination', 'distance', 'tc']

model = MarginalizedPhaseGaussianNoise(variable_params, stilde, low_frequency_cutoff, \
                                              psds=psds, high_frequency_cutoff=high_frequency_cutoff, static_params=static_params)

#-- defining loglikelihood function ---
def pycbc_log_likelihood(query):
    
    mchirp, mass_ratio, s1z, s2z, iota, distance, tc = query
    m1 = mass1_from_mchirp_q(mchirp, mass_ratio)
    m2 = mass2_from_mchirp_q(mchirp, mass_ratio)
    
    model.update(mass1=m1, mass2=m2, spin1z=s1z, spin2z=s2z, inclination=iota, distance=distance, tc=tc)

    return model.loglr

#-- defining prior tranform ---
mchirp_min, mchirp_max = 1.197, 1.198
mass_ratio_min, mass_ratio_max = 1, 1.7
s1z_min, s1z_max = 0, 0.05
s2z_min, s2z_max = 0, 0.05
distance_min, distance_max = 12, 53
tc_min, tc_max = merger.time - 0.15, merger.time + 0.15

def prior_transform(cube):
    
    """
    chirpmass and q: distribution which is uniform in m1 and m2 and constrained by chirpmass and q
    spin1z/2z: uniform distribtion
    inclination: uniform in cos(iota)
    distance: uniform volume
    tc: uniform distribution
    """
    cube[0] = np.power((mchirp_max**2-mchirp_min**2)*cube[0]+mchirp_min**2,1./2)     # chirpmass: power law mc**1
    cube[1] = cdfinv_q(mass_ratio_min, mass_ratio_max, cube[1])                      # mass-ratio: uniform prior
    cube[2] = s1z_min + (s1z_max - s1z_min) * cube[2]   # s1z: uniform prior
    cube[3] = s2z_min + (s2z_max - s2z_min) * cube[3]   # s2z: uniform prior
    cube[4] = np.arccos(2*cube[4] - 1) 
    cube[5] = np.power((distance_max**3-distance_min**3)*cube[5]+distance_min**3,1./3) # distance: unifrom prior in dL**3
    cube[6] = tc_min + (tc_max - tc_min) * cube[6] # pol: uniform angle

    return cube

print('********** Sampling starts *********\n')

nProcs = int(input('no. of processors to be used for dynesty sampler: '))
nDims = 7

st = time.time()

#-- definig dynesty sampler ---
with mp.Pool(nProcs) as pool:
    
    sampler = dynesty.DynamicNestedSampler(pycbc_log_likelihood, prior_transform, nDims, sample='rwalk', \
                                            pool=pool, queue_size=nProcs)
    sampler.run_nested(dlogz_init=1e-4)
                                           
#-- saving pe samples ---
raw_samples = sampler.results['samples']
print('Evidence:{}'.format(res['logz'][-1]))
file = h5py.File('samples_data_pycbc_{}_fISCO.hdf5'.format(whichfrac), 'w')
params = ['mchirp', 'mass_ratio', 's1z', 's2z', 'iota', 'distance', 'tc', 'logwt', 'logz', 'logl']
i = 0
for p in params:
    file.create_dataset(p, data=raw_samples[:,i])
    i = i + 1
file.close()

et = time.time()

print('Done!!!')
print('Time taken:{} Hours.'.format((et-st)/3600.))


