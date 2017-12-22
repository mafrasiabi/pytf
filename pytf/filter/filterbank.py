from __future__ import division
""" A module for filter bank.
"""
# Authors : David C.C. Lu <davidlu89@gmail.com>
#
# License : BSD (3-clause)
import warnings
import numpy as np
from scipy.signal import (get_window, group_delay)
from pyfftw.interfaces.numpy_fft import (rfft, irfft, ifft, fftfreq)

from .filter import (create_filter, get_center_frequencies, get_frequency_of_interests, get_all_frequencies)
from ..reconstruction.overlap import (overlap_add)
from ..time_frequency.stft import (_check_winsize, stft)
from ..utilities.parallel import (Parallel, ParallelDummy)

def _is_uniform_distributed_cf(cf):
    """ Check if the provided center frequencies are uniformly distributed.
    """
    return np.any(np.diff(np.diff(cf))!=0)

class FilterBank(object):
    """ Create a filter bank object for signal processing.

    Parameters:
    -----------
    binsize: int (default: 1024)
        The number of samples used for each analysis window for STFT.

    decimate_by: int (default: 1)
        The decimating factor.

    nprocs: int (default:1)
        The number of processes for filtering.

    domain: str (default: 'freq')
        Specify if the return to be in frequency domain ('freq'), or time domain ('time').

    bandwidth: float (default: None)
        The bandwidth of the filter. In this case, it's twice the cutoff frequency of the lowpass filter.

    center_freqs: ndarray (default: None)
        The center frequencies of each frequency bands of interest.

    freq_bands: ndarray (default: None)
        The frequency bands of interest.

    order: int (default: None)
        The order of the filter.

    sample_rate: int (default: None)
        The sample rate of the signal and the filter.

    filt: bool (default: False)
        If False, no filter will be used.
        If True, the pre-defined filter will be used.

    hilbert: bool (default: False)
        If False, the output signal is real.
        If True, the output signal is analytical (real and imaginary).

    kwargs:
        The key-word arguments for initializing multiprocess. See self.init_multiprocess().
    """
    def __init__(self, mprocs=False, nch=1, nsamp=2**14, binsize=2**10, decimate_by=1, nprocs=1, domain='time', \
                 bandwidth=None, center_freqs=None, freq_bands=None, order=None, sample_rate=None, \
                 hilbert=False, **kwargs):

        # Pre-defined Parameters
        self._factor = .6
        _overlap_factor = 0.5

        # Filter Output Parameters
        self.hilbert = hilbert
        self.domain = domain

        # Signal Parameters
        self._nch = nch
        self._decimate_by = decimate_by
        self._nsamp = nsamp
        # self._nsamp = int(((self._nwin - 1) * _overlap_factor) * self._binsize)

        # Overlap-Window Parameters
        self._binsize = binsize
        self._nwin = int((self.nsamp / self._binsize) / _overlap_factor + 1)

        # Frequency Parameters
        self._sample_rate = sample_rate
        self._center_freqs, self._bandwidth, self._freq_bands = get_all_frequencies(center_freqs, bandwidth, freq_bands)

        self._nfreqs = self.freq_bands.shape[0]
        self._interval_per_hz = self._binsize / self.sample_rate # interval per Hz

        # The decimated sample size
        self._binsize_ = self._binsize // self.decimate_by

        # Create indices for efficiently filtering the signal
        self._get_indices_for_frequency_shifts()

        # Create a prototype filter
        self._order = order
        self.filts = self._create_prototype_filter(shift=True, output='freq')

        self._delay = self.delayed_samples()
        self._delay_ = self.delay // self.decimate_by

        # Initializing for multiprocessing
        self._nprocs = nprocs
        self._mprocs = True if self.nprocs > 1 else mprocs

        self.init_multiprocess(ins_shape=[(self.nch, self._nwin, self._binsize//2 + 1),\
                                          (self.nfreqs, int((self.bandwidth * self._factor) * 2 * self.interval_per_hz)),
                                          (self.nfreqs, int((self.bandwidth * self._factor) * 2 * self.interval_per_hz)),
                                          (self.nfreqs, int((self.bandwidth * self._factor) * 2 * self.interval_per_hz))],\
                                out_shape=(self.nch, self._nwin, self.nfreqs, self._binsize // self.decimate_by))

    def init_multiprocess(self, ins_shape=None, out_shape=None,
                                ins_dtype=[np.complex64, np.int32, np.int32, np.int32],
                                out_dtype=np.float32):
        """ Initializing multiprocessing used in this filter bank class.

        Parameters:
        ----------
        ins_shape: tuple (default: None)
            The shape of the input array, STFT of x.

        out_shape: tuple (default: None)
            The shape of the output array, the filtered signal (nch, nfreqs, nwin x binsize_).
        """
        ndtype = np.complex64 if self.hilbert else out_dtype
        if self.mprocs:
            self._pfunc = Parallel(
                            self._fft_procs, nprocs=self.nprocs, axis=2,
                            ins_shape = ins_shape,
                            out_shape = out_shape,
                            ins_dtype = ins_dtype,
                            out_dtype = ndtype,
                            dtype = ndtype,
                            filts = self.filts,
                            nfreqs = self.nfreqs
                        )
        else:
            warnings.warn("The multiprocessing is disabled! To enable multiprocessing, "+\
                        "specify 'ins_shape' and 'out_shape' for preallocating shared memory.")

            self._pfunc = ParallelDummy(self._fft_procs,
                                dtype=ndtype,
                                filts = self.filts,
                                nfreqs = self.nfreqs
                            )

    def kill(self, opt=None): # kill the multiprocess
        """ Killing all the multiprocessing processes.
        """
        self._pfunc.kill(opt=opt)

    def analysis(self, x, window='hamming'):
        """ Generate the analysis bank.

        Parameters:
        -----------
        x: ndarray, (nch x nsamp)
            The input signal.

        window: str (default: 'hamming')
            The window used to create overlapping slices of the time domain signal.
        """
        ndtype = np.complex64 if self.hilbert else np.float32

        nch, nsamp = x.shape
        nsamp //= self.decimate_by

        X = stft(x, binsize=self._binsize, window=window, axis=-1, \
                    planner_effort='FFTW_ESTIMATE') / self.decimate_by

        x_ = self._pfunc.result(X, self._idx1, self._idx2, self._fidx)
        x_ = np.concatenate([x_[:,:,:,self.delay_:], x_[:,:,:,:self.delay_]], axis=-1)\
                if self.filts is not None else x_

        # Reconstructing the signal using overlap-add
        _x = overlap_add(x_, self._binsize_, overlap_factor=.5, dtype=ndtype)
        return _x[:,:,self._binsize_//2:nsamp+self._binsize_//2]

    def synthesis(self, x, **kwargs):
        """ TODO: Reconstruct the signal from the analysis bank.
        """
        return

    def _fft_procs(self, X, idx1, idx2, fidx, filts=None, nfreqs=None, \
                        slices_idx=[slice(None)]*4, dtype=np.float32):
        """ FFT filtering using STFT on the signal.

        Paramters:
        ----------
        X: ndarray (nch x nwin x nsamp)
            The STFT of the signal to be analyzed.

        idx1: ndarray
            The fancy index on X. This demodulates the signal. See self._get_indices_for_frequency_shifts().

        idx2: ndarray
            The fancy index for reconstructing X_. This modulates
            the demodulated signal. See self._get_indices_for_frequency_shifts().

        fidx: ndarray
            The fancy index for slicing the specific frequency components from the frequency
            response of the filter coefficients. See self._get_indices_for_frequency_shifts().

        slices_idx: list
            This argument is only needed when implementing in the Parallel class.
            This specifies how to split the given indices into the number of processes as evenly as possible.

        dtype: ndarray type (default: np.float32)
            The ndarray type of the signal output.
        """
        nch, nwin, nsamp = X.shape
        X_ = np.zeros((nch, nwin, nfreqs, self._binsize_//2), dtype=np.complex64)
        X_[:,:,idx2,idx1] = X[:,:,idx1] * filts[fidx]

        if dtype == np.float32:
            _ifft = irfft
        else:
            X_[:,:,:,1:] *= 2
            _ifft = ifft

        if self.domain == 'freq':
            return X_

        elif self.domain == 'time':
            return _ifft(X_[slices_idx], n=self._binsize_, axis=-1, planner_effort='FFTW_ESTIMATE')

    def delayed_samples(self):
        filt = self._create_prototype_filter(output='time')
        return int(np.mean(group_delay([filt,1])[1]))

    def _create_prototype_filter(self, **kwargs):
        """ Create the prototype filter, which is the only filter require for
        windowing in the frequency domain of the signal. This filter is a lowpass filter.
        """
        tmp = create_filter(self.order, self.bandwidth/2., self.sample_rate/2., self._binsize, **kwargs)
        if kwargs['output'] == 'time':
            return tmp
        elif kwargs['output'] == 'freq':
            return tmp[1]

    def _get_indices_for_frequency_shifts(self):
        """ Get the indices for properly shifting the fft of signal to DC, and the indices
        for shifting the fft of signal back to the correct frequency indices for ifft.
        """
        fois_ix_ = np.asarray(self.freq_bands * self.interval_per_hz, dtype=np.int32)
        cf_ix_ = np.asarray(self.center_freqs * self.interval_per_hz, dtype=np.int32)

        # Get indices for filter coeffiecients
        self._fidx = np.zeros((self.nfreqs, int((self.bandwidth * self._factor) * 2 * self.interval_per_hz)), dtype=np.int32)
        cf0 = self._binsize // 2
        for ix, f_ix in enumerate(fois_ix_):
            l_bound = cf0 - int(self.interval_per_hz * self.bandwidth * self._factor)

            diff = self._fidx[ix,:].shape[-1] - np.arange(l_bound, l_bound + (self.bandwidth * self._factor) * 2 * self.interval_per_hz).size
            self._fidx[ix,:] = np.arange(l_bound, l_bound + (self.bandwidth * self._factor) * 2 * self.interval_per_hz + diff)

        self._fidx = np.asarray(self._fidx, dtype=np.int32)

        # Code 1: Does the same thing as below
        x = np.arange(0, int((self.bandwidth * self._factor) * 2 * self.interval_per_hz))
        y = np.arange(0, self.nfreqs)
        index1, index2 = np.meshgrid(x, y)

        index1 += (np.atleast_2d(cf_ix_) - int(self.interval_per_hz * self.bandwidth * self._factor))
        self._idx1 = np.asarray(index1, dtype=np.int32)
        self._idx2 = np.asarray(index2, dtype=np.int32)

    @property
    def freq_bands(self):
        return self._freq_bands

    @property
    def center_freqs(self):
        return self._center_freqs

    @property
    def bandwidth(self):
        return self._bandwidth

    @property
    def nfreqs(self):
        return self._nfreqs

    @property
    def sample_rate(self):
        return self._sample_rate

    @property
    def order(self):
        return self._order

    @property
    def interval_per_hz(self):
        return self._interval_per_hz

    @property
    def nprocs(self):
        return self._nprocs

    @property
    def mprocs(self):
        return self._mprocs

    @property
    def delay(self):
        return self._delay

    @property
    def delay_(self):
        return self._delay_

    @property
    def nch(self):
        return self._nch

    @property
    def decimate_by(self):
        return self._decimate_by

    @property
    def nsamp(self):
        return self._nsamp
