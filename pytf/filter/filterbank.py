from __future__ import division
"""A module for filter bank.
"""
# Authors : David C.C. Lu <davidlu89@gmail.com>
#
# License : BSD (3-clause)
import time
import warnings
import numpy as np
from scipy.signal import get_window
from pyfftw.interfaces.numpy_fft import (rfft, irfft, ifft, fftfreq)

from .filter import (create_filter, get_center_frequencies, get_frequency_of_interests, get_all_frequencies)
from ..reconstruction.overlap import (overlap_add)
from ..time_frequency.stft import (_check_winsize, stft)
from ..utilities.parallel import Parallel

def _is_uniform_distributed_cf(cf):
    """
    Check if the provided center frequencies are uniformly distributed.
    """
    return np.any(np.diff(np.diff(cf))!=0)

class FilterBank(object):
    """
    """
    def __init__(self, binsize=1024, decimate_by=1, nprocs=1, domain='time',
                 bw=None, cf=None, foi=None, order=None, sfreq=None, filt=True, hilbert=False, \
                 **kwargs):

        self.decimate_by = decimate_by
        self.hilbert = hilbert
        self.domain = domain
        # Create indices for overlapping window
        self.binsize = binsize

        # The decimated sample size
        self.binsize_ = self.binsize // self.decimate_by

        # Organize frequencies
        self._center_f, self._bandwidth, self._foi = get_all_frequencies(cf, bw, foi)

        self._nfreqs = self.foi.shape[0]

        self._sfreq = sfreq
        self._int_phz = self.binsize / self.sfreq # interval per Hz

        # Create indices for efficiently filtering the signal
        self._get_indices_for_frequency_shifts()

        # Create a prototype filter
        self._order = order
        self.filts = self._create_prototype_filter(self.order, self.bandwidth/2., self.sfreq, self.binsize)\
                        if filt else None

        # Initializing for multiprocessing
        self.nprocs = nprocs
        self.mprocs = False
        self.kwargs = kwargs
        if list(self.kwargs.keys()) != ['ins_shape', 'out_shape']:
            if self.nprocs > 1:
                warnings.warn("Must initialize 'ins_shape' and 'out_shape' using FilterBank.init_multiprocess(), if you want to use multiple processes!")

        else:
            self.init_multiprocess(**kwargs)

    def init_multiprocess(self, ins_shape=None, out_shape=None):
        self.mprocs = True
        ins_dtype = [np.complex64, np.int32, np.int32, np.int32]
        ndtype = np.complex64 if self.hilbert else np.float32
        self.pfunc = Parallel(self._fft_procs,
                     nprocs = self.nprocs, axis = 2,
                     ins_shape = ins_shape,
                     out_shape = out_shape,
                     ins_dtype = ins_dtype,
                     out_dtype = ndtype,
                     dtype = ndtype)


    def kill(self, opt=None): # kill the multiprocess
        self.pfunc.kill(opt=opt)

    def analysis(self, x, nsamp=None, window='hamming'):
        """
        Generate the analysis bank.

        Parameters:
        -----------
        x: ndarray, (nch x nsamp)
            The input signal.

        filt: ndarray (default: False)
            If False, no filter will be used.
            If True, the pre-defined filter will be used.

        window: str (default: 'hanning')
            The window used to create overlapping slices of the time domain signal.

        domain: str (default: 'freq')
            Specify if the return to be in frequency domain ('freq'), or time domain ('time').

        kwargs:
            The key-word arguments for pyfftw.
        """
        ndtype = np.complex64 if self.hilbert else np.float32

        nch, nsamp = x.shape
        nsamp //= self.decimate_by

        X = stft(x, binsize=self.binsize, window=window, axis=-1, planner_effort='FFTW_ESTIMATE') / self.decimate_by

        func = self.pfunc.result if self.mprocs else self._fft_procs

        t0 = time.time()
        x_ = func(X, self._idx1, self._idx2, self._fidx, dtype=ndtype)
        print('Time: {} [ms]'.format(round(1E3*(time.time()-t0),3)))

        # Reconstructing the signal using overlap-add
        _x = overlap_add(x_, self.binsize_, overlap_factor=.5, dtype=ndtype)
        return _x[:,:,self.binsize_//2:nsamp+self.binsize_//2]

    def synthesis(self, x, **kwargs):
        """
        TODO: Reconstruct the signal from the analysis bank.
        """
        return

    def _fft_procs(self, X, idx1, idx2, fidx, slices_idx=[slice(None)]*4, dtype=np.float32):
        """
        FFT filtering using STFT on the signal.

        Paramters:
        ----------
        x: ndarray (nch x nsamp)
            The signal to be analyzed.
        """
        t0 = time.time()
        nch, nwin, nsamp = X.shape
        X_ = np.zeros((nch, nwin, self.nfreqs, self.binsize_//2), dtype=np.complex64)
        if self.filts is None:
            X_[:,:,idx2,idx1] = X[:,:,idx1]
        else:
            X_[:,:,idx2,idx1] = X[:,:,idx1] * self.filts[fidx]

        if dtype == np.float32:
            _ifft = irfft
        else:
            X_[:,:,:,1:] *= 2
            _ifft = ifft

        if self.domain == 'freq':
            return X_

        elif self.domain == 'time':
            x_ = _ifft(X_[slices_idx], n=self.binsize_, axis=-1, planner_effort='FFTW_ESTIMATE')
            print('time: {} [ms]'.format(round((time.time() - t0)*1E3, 3)))
            return x_

    @property
    def foi(self):
        return self._foi

    @property
    def center_f(self):
        return self._center_f

    @property
    def bandwidth(self):
        return self._bandwidth

    @property
    def nfreqs(self):
        return self._nfreqs

    @property
    def sfreq(self):
        return self._sfreq

    @property
    def order(self):
        return self._order

    @property
    def int_phz(self):
        return self._int_phz

    def _create_prototype_filter(self, order, f_cut, fs, N):
        """
        Create the prototype filter, which is the only filter require for windowing in the frequency
        domain of the signal. This filter is a lowpass filter.
        """
        _, filts = create_filter(order, f_cut, fs/2., N, shift=True)

        return filts

    def _get_indices_for_frequency_shifts(self):
        """
        Get the indices for properly shifting the fft of signal to DC, and the indices for shifting
        the fft of signal back to the correct frequency indices for ifft.
        """
        fois_ix_ = np.asarray(self.foi * self._int_phz, dtype=np.int64)

        # Get indices for filter coeffiecients
        self._fidx = np.zeros((self.nfreqs, int(self.bandwidth * 2 * self._int_phz)), dtype=np.int64)
        cf0 = self.binsize // 2
        for ix, f_ix in enumerate(fois_ix_):

            if f_ix[0] <= self.bandwidth:
                l_bound = cf0 - int(self._int_phz * self.bandwidth // 4) - 1
            else:
                l_bound = cf0 - int(self._int_phz * self.bandwidth) - 1

            diff = self._fidx[ix,:].shape[-1] - np.arange(l_bound, l_bound + self.bandwidth * 2 * self._int_phz).size

            self._fidx[ix,:] = np.arange(l_bound, l_bound + self.bandwidth * 2 * self._int_phz + diff)

        self._fidx = np.asarray(self._fidx, dtype=np.int32)

        # Code 1: Does the same thing as below
        x = np.arange(0, int(self.bandwidth * 2 * self._int_phz))
        y = np.arange(0, self.nfreqs)
        index1, index2 = np.meshgrid(x, y)
        index1 += np.atleast_2d(fois_ix_[:,0]).T
        self._idx1 = np.asarray(index1, dtype=np.int32)
        self._idx2 = np.asarray(index2, dtype=np.int32)
