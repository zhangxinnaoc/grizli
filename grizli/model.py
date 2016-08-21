"""
Model grism spectra in individual FLTs   
"""
import os
import collections
import copy

import numpy as np
import scipy.ndimage as nd
import matplotlib.pyplot as plt

import astropy.io.fits as pyfits
from astropy.table import Table
import astropy.wcs as pywcs

#import stwcs

### Helper functions from a document written by Pirzkal, Brammer & Ryan 
from . import grismconf
from . import utils
from .utils_c import disperse
from .utils_c import interp

### Factors for converting HST countrates to Flamba flux densities
photflam_list = {'F098M': 6.0501324882418389e-20, 
            'F105W': 3.038658152508547e-20, 
            'F110W': 1.5274130068787271e-20, 
            'F125W': 2.2483414275260141e-20, 
            'F140W': 1.4737154005353565e-20, 
            'F160W': 1.9275637653833683e-20, 
            'F435W': 3.1871480286278679e-19, 
            'F606W': 7.8933594352047833e-20, 
            'F775W': 1.0088466875014488e-19, 
            'F814W': 7.0767633156044843e-20, 
            'VISTAH':1.9275637653833683e-20*0.95,
            'GRISM': 1.e-20}
 
### Filter pivot wavelengths
photplam_list = {'F098M': 9864.722728110915, 
            'F105W': 10551.046906405772, 
            'F110W': 11534.45855553774, 
            'F125W': 12486.059785775655, 
            'F140W': 13922.907350356367, 
            'F160W': 15369.175708965562,
            'F435W': 4328.256914042873, 
            'F606W': 5921.658489236346,
            'F775W': 7693.297933335407,
            'F814W': 8058.784799323767,
            'VISTAH':1.6433e+04,
            'GRISM': 1.6e4}

# character to skip clearing line on STDOUT printing
no_newline = '\x1b[1A\x1b[1M' 

### Demo for computing photflam and photplam with pysynphot
if False:
    import pysynphot as S
    n = 1.e-20
    spec = S.FlatSpectrum(n, fluxunits='flam')
    photflam_list = {}
    photplam_list = {}
    for filter in ['F098M', 'F105W', 'F110W', 'F125W', 'F140W', 'F160W', 'G102', 'G141']:
        bp = S.ObsBandpass('wfc3,ir,%s' %(filter.lower()))
        photplam_list[filter] = bp.pivot()
        obs = S.Observation(spec, bp)
        photflam_list[filter] = n/obs.countrate()
        
    for filter in ['F435W', 'F606W', 'F775W', 'F814W']:
        bp = S.ObsBandpass('acs,wfc1,%s' %(filter.lower()))
        photplam_list[filter] = bp.pivot()
        obs = S.Observation(spec, bp)
        photflam_list[filter] = n/obs.countrate()

class GrismDisperser(object):
    def __init__(self, id=0, direct=np.zeros((20,20), dtype=np.float32), 
                       segmentation=None, origin=[500, 500], 
                       xcenter=0., ycenter=0., pad=0, grow=1, beam='A',
                       conf=['WFC3','F140W', 'G141']):
        """Object for computing dispersed model spectra
        
        Parameters
        ----------
        id: int
            Only consider pixels in the segmentation image with value `id`.  
            Default of zero to match the default empty segmentation image.
        
        direct: ndarray
            Direct image cutout in f_lambda units (i.e., e-/s times PHOTFLAM).
            Default is a trivial zeros array.
        
        segmentation: ndarray(float32) or None
            Segmentation image.  If None, create a zeros array with the same 
            shape as `direct`.
        
        origin: [int,int]
            `origin` defines the lower left pixel index (y,x) of the `direct` 
            cutout from a larger detector-frame image
        
        xcenter, ycenter: float, float
            Sub-pixel centering of the exact center of the object, relative
            to the center of the thumbnail.  Needed for getting exact 
            wavelength grid correct for the extracted 2D spectra.
            
        pad: int
            Offset between origin = [0,0] and the true lower left pixel of the 
            detector frame.  This can be nonzero for cases where one creates
            a direct image that extends beyond the boundaries of the nominal
            detector frame to model spectra at the edges.
        
        grow: int >= 1
            TBD
        
        beam: str
            Spectral order to compute.  Must be defined in `self.conf.beams`
        
        conf: [str,str,str] or `grismconf.aXeConf` object.
            Pre-loaded aXe-format configuration file object or if list of 
            strings determine the appropriate configuration filename with 
            `grismconf.get_config_filename` and load it.
            
        Useful Attributes
        -----------------
        sh: 2-tuple
            shape of the direct array
        
        sh_beam: 2-tuple
            computed shape of the 2D spectrum
        
        seg: ndarray
            segmentation array
        
        lam: array
            wavelength along the trace

        ytrace: array
            y pixel center of the trace.  Has same dimensions as sh_beam[1]. 
        
        sensitivity: array
            conversion factor from native e/s to f_lambda flux densities
          
        modelf, model: array, ndarray
            2D model spectrum.  `model` is linked to `modelf` with "reshape", 
            the later which is a flattened 1D array where the fast 
            calculations are actually performed.
        
        model: ndarray
            2D model spectrum linked to `modelf` with reshape.
        
        slx_parent, sly_parent: slices
            slices defined relative to `origin` to match the location of the 
            computed 2D spectrum.
            
        total_flux: float
            Total f_lambda flux in the thumbail within the segmentation 
            region.
        """
        
        self.id = id
        
        ### lower left pixel of the `direct` array in native detector
        ### coordinates
        self.origin = origin
        self.pad = pad
        self.grow = grow
        
        ### Direct image
        self.direct = direct
        self.sh = self.direct.shape
        if self.direct.dtype is not np.float32:
            self.direct = np.cast[np.float32](self.direct)
        
        ### Segmentation image, defaults to all zeros
        if segmentation is None:
            self.seg = np.zeros_like(self.direct, dtype=np.float32)
        else:
            self.seg = segmentation
            if self.seg.dtype is not np.float32:
                self.seg = np.cast[np.float32](self.seg)
                
        if isinstance(conf, list):
            conf_f = grismconf.get_config_filename(conf[0], conf[1], conf[2])
            self.conf = grismconf.load_grism_config(conf_f)
        else:
            self.conf = conf
        
        ### Initialize attributes
        self.xc = self.sh[1]/2+self.origin[1]
        self.yc = self.sh[0]/2+self.origin[0]
        
        ### Sub-pixel centering of the exact center of the object, relative
        ### to the center of the thumbnail
        self.xcenter = xcenter
        self.ycenter = ycenter
        
        self.beam = beam
        
        ### Get dispersion parameters at the reference position
        self.dx = self.conf.dxlam[self.beam] #+ xcenter #-xoff
        if self.grow > 1:
            self.dx = np.arange(self.dx[0]*self.grow, self.dx[-1]*self.grow)
            
        self.ytrace_beam, self.lam_beam = self.conf.get_beam_trace(
                                    x=(self.xc+xcenter-self.pad)/self.grow,
                                    y=(self.yc+ycenter-self.pad)/self.grow,
                                    dx=(self.dx+xcenter*0-0.5)/self.grow,
                                    beam=self.beam)
        
        self.ytrace_beam *= self.grow
        
        ### Integer trace
        # Add/subtract 20 for handling int of small negative numbers    
        dyc = np.cast[int](self.ytrace_beam+20)-20+1 
        
        ### Account for pixel centering of the trace
        self.yfrac_beam = self.ytrace_beam - np.floor(self.ytrace_beam)
        
        ### Interpolate the sensitivity curve on the wavelength grid. 
        ysens = self.lam_beam*0
        so = np.argsort(self.lam_beam)
        ysens[so] = interp.interp_conserve_c(self.lam_beam[so],
                                 self.conf.sens[self.beam]['WAVELENGTH'], 
                                 self.conf.sens[self.beam]['SENSITIVITY'])
        self.lam_sort = so
        
        ### Needs term of delta wavelength per pixel for flux densities
        dl = np.abs(np.append(self.lam_beam[1] - self.lam_beam[0],
                              np.diff(self.lam_beam)))
        ysens *= 1.e-17*dl
        
        self.sensitivity_beam = ysens
        
        ### Initialize the model arrays
        self.NX = len(self.dx)
        self.sh_beam = (self.sh[0], self.sh[1]+self.NX)
        
        self.modelf = np.zeros(np.product(self.sh_beam), dtype=np.float)
        self.model = self.modelf.reshape(self.sh_beam)
        self.idx = np.arange(self.modelf.size).reshape(self.sh_beam)
        
        ## Indices of the trace in the flattened array 
        self.x0 = np.array(self.sh)/2
        self.dxpix = self.dx - self.dx[0] + self.x0[1] #+ 1
        try:
            self.flat_index = self.idx[dyc + self.x0[0], self.dxpix]
        except IndexError:
            #print 'Index Error', id, self.x0[0], self.xc, self.yc, self.beam, self.ytrace_beam.max(), self.ytrace_beam.min()
            raise IndexError
            
        ###### Trace, wavelength, sensitivity across entire 2D array
        self.dxfull = np.arange(self.sh_beam[1], dtype=int) 
        self.dxfull += self.dx[0]-self.x0[1]
        
        # self.ytrace, self.lam = self.conf.get_beam_trace(x=self.xc,
        #                  y=self.yc, dx=self.dxfull, beam=self.beam)
        
        self.ytrace, self.lam = self.conf.get_beam_trace(
                                    x=(self.xc+xcenter-self.pad)/self.grow,
                                    y=(self.yc+ycenter-self.pad)/self.grow,
                                    dx=(self.dxfull+xcenter-0.5)/self.grow,
                                    beam=self.beam)
        
        self.ytrace *= self.grow
        
        ysens = self.lam*0
        so = np.argsort(self.lam)
        ysens[so] = interp.interp_conserve_c(self.lam[so],
                                 self.conf.sens[self.beam]['WAVELENGTH'], 
                                 self.conf.sens[self.beam]['SENSITIVITY'])
        
        dl = np.abs(np.append(self.lam[1] - self.lam[0],
                              np.diff(self.lam)))
        ysens *= 1.e-17*dl
        self.sensitivity = ysens
        
        self.total_flux = self.direct[self.seg == self.id].sum()
        
        ## Slices of the parent array based on the origin parameter
        self.slx_parent = slice(self.origin[1] + self.dxfull[0] + self.x0[1],
                            self.origin[1] + self.dxfull[-1] + self.x0[1]+1)
        
        self.sly_parent = slice(self.origin[0], self.origin[0] + self.sh[0])
        
        self.spectrum_1d =  None
        
    def compute_model(self, id=None, thumb=None, spectrum_1d=None,
                      in_place=True, outdata=None):
        """Compute a model 2D grism spectrum

        Parameters
        ----------
        id: int
            Only consider pixels in the segmentation image (`self.seg`) with 
            values equal to `id`.
        
        thumb: array with shape = `self.sh` or None
            Optional direct image.  If `None` then use `self.direct`.
        
        spectrum_1d: [array, array] or None
            Optional 1D template [wave, flux] to use for the 2D grism model.
            If `None`, then implicitly assumes flat f_lambda spectrum.
                
        in_place: bool
            If True, put the 2D model in `self.model` and `self.modelf`, 
            otherwise put the output in a clean array or preformed `outdata`. 
            
        outdata: array with shape = `self.sh_beam`
            Preformed array to which the 2D model is added, if `in_place` is
            False.
            
        Returns
        -------
        model: ndarray
            If `in_place` is False, returns the 2D model spectrum.  Otherwise
            the result is stored in `self.model` and `self.modelf`.
        """
        
        if id is None:
            id = self.id
        else:
            self.id = id
            
        ### Template (1D) spectrum interpolated onto the wavelength grid
        if in_place:
            self.spectrum_1d = spectrum_1d
        
        if spectrum_1d is not None:
            xspec, yspec = spectrum_1d
            scale_spec = self.sensitivity_beam*0.
            int_func = interp.interp_conserve_c
            scale_spec[self.lam_sort] = int_func(self.lam_beam[self.lam_sort],
                                                xspec, yspec)
        else:
            scale_spec = 1.
            
        ### Output data, fastest is to compute in place but doesn't zero-out
        ### previous result                    
        if in_place:
            self.modelf *= 0
            outdata = self.modelf
        else:
            if outdata is None:
                outdata = self.modelf*0
                
        ### Optionally use a different direct image
        if thumb is None:
            thumb = self.direct
        else:
            if thumb.shape != self.sh:
                print """
Error: `thumb` must have the same dimensions as the direct image! (%d,%d)      
                """ %(self.sh[0], self.sh[1])
                return False

        ### Now compute the dispersed spectrum using the C helper
        status = disperse.disperse_grism_object(thumb, self.seg, id,
                                 self.flat_index, self.yfrac_beam,
                                 self.sensitivity_beam*scale_spec,
                                 outdata, self.x0, np.array(self.sh),
                                 self.x0, np.array(self.sh_beam))

        if not in_place:
            return outdata
        else:
            return True
    
    def optimal_extract(self, data, bin=0, ivar=1.):        
        """Horne (1986) optimally-weighted 1D extraction
        
        Parameters
        ----------
        data: ndarray with shape == `self.sh_beam`
            2D data to extract
        
        bin: int, optional
            Simple boxcar averaging of the output 1D spectrum
        
        ivar: float or ndarray with shape == `self.sh_beam`
            Inverse variance array or scalar float that multiplies the 
            optimal weights
            
        Returns
        -------
        wave, opt_flux, opt_rms: array-like
            `wave` is the wavelength of 1D array
            `opt_flux` is the optimally-weighted 1D extraction
            `opt_rms` is the weighted uncertainty of the 1D extraction
            
            All are optionally binned in wavelength if `bin` > 1.
        """
        import scipy.ndimage as nd
               
        if not hasattr(self, 'optimal_profile'):
            m = self.compute_model(id=self.id, in_place=False)
            m = m.reshape(self.sh_beam)
            m[m < 0] = 0
            self.optimal_profile = m/m.sum(axis=0)
        
        if data.shape != self.sh_beam:
            print """
`data` (%d,%d) must have the same shape as the data array (%d,%d)
            """ %(data.shape[0], data.shape[1], self.sh_beam[0], 
                  self.sh_beam[1])
            return False

        if not isinstance(ivar, float):
            if ivar.shape != self.sh_beam:
                print """
`ivar` (%d,%d) must have the same shape as the data array (%d,%d)
                """ %(ivar.shape[0], ivar.shape[1], self.sh_beam[0], 
                      self.sh_beam[1])
                return False
                
        num = self.optimal_profile*data*ivar
        den = self.optimal_profile**2*ivar
        opt_flux = num.sum(axis=0)/den.sum(axis=0)
        opt_var = 1./den.sum(axis=0)
                
        if bin > 1:
            kern = np.ones(bin, dtype=float)/bin
            opt_flux = nd.convolve(opt_flux, kern)[bin/2::bin]
            opt_var = nd.convolve(opt_var, kern**2)[bin/2::bin]
            wave = self.lam[bin/2::bin]
        else:
            wave = self.lam
                
        opt_rms = np.sqrt(opt_var)
        opt_rms[opt_var == 0] = 0
        
        return wave, opt_flux, opt_rms
    
    def contained_in_full_array(self, full_array):
        """Check if subimage slice is fully contained within larger array
        """
        sh = full_array.shape
        if (self.sly_parent.start < 0) | (self.slx_parent.start < 0):
            return False
        
        if (self.sly_parent.stop >= sh[0]) | (self.slx_parent.stop >= sh[1]):
            return False
        
        return True
        
    def add_to_full_image(self, data, full_array):
        """Add spectrum cutout back to the full array
        
        Parameters
        ----------
        data: ndarray shape `self.sh_beam` (e.g., self.model)
            Spectrum cutout
        
        full_array: ndarray
            Full detector array, where the lower left pixel of `data` is given
            by `origin`.
        
        `data` is *added* to `full_array` in place, so, for example, to 
        subtract `self.model` from the full array, call the function with 
        
        >>> self.add_to_full_image(-self.model, full_array)
         
        """
        
        if self.contained_in_full_array(full_array):
            full_array[self.sly_parent, self.slx_parent] += data
        else:    
            sh = full_array.shape
        
            xpix = np.arange(self.sh_beam[1])
            xpix += self.origin[1] + self.dxfull[0] + self.x0[1]
        
            ypix = np.arange(self.sh_beam[0])
            ypix += self.origin[0]
        
            okx = (xpix >= 0) & (xpix < sh[1])
            oky = (ypix >= 0) & (ypix < sh[1])
        
            if (okx.sum() == 0) | (oky.sum() == 0):
                return False
        
            sly = slice(ypix[oky].min(), ypix[oky].max()+1)
            slx = slice(xpix[okx].min(), xpix[okx].max()+1)
            full_array[sly, slx] += data[oky,:][:,okx]

        #print sly, self.sly_parent, slx, self.slx_parent
        return True
    
    def cutout_from_full_image(self, full_array):
        """Get beam-sized cutout from a full image
        
        Parameters
        ----------
        full_array: ndarray
            Array of the size of the parent array from which the cutout was 
            extracted.  If possible, the function first tries the slices with
            
                `full_array[self.sly_parent, self.slx_parent]`
            
            and then computes smaller slices for cases where the beam spectrum
            falls off the edge of the parent array.
        
        Returns
        -------
        cutout: ndarray 
            Array with dimensions of `self.model`.
        
        """
        #print self.sly_parent, self.slx_parent, full_array.shape
        
        if self.contained_in_full_array(full_array):
            data = full_array[self.sly_parent, self.slx_parent]
        else:
            sh = full_array.shape
            ### 
            xpix = np.arange(self.sh_beam[1])
            xpix += self.origin[1] + self.dxfull[0] + self.x0[1]
        
            ypix = np.arange(self.sh_beam[0])
            ypix += self.origin[0]
        
            okx = (xpix >= 0) & (xpix < sh[1])
            oky = (ypix >= 0) & (ypix < sh[1])
        
            if (okx.sum() == 0) | (oky.sum() == 0):
                return False
        
            sly = slice(ypix[oky].min(), ypix[oky].max()+1)
            slx = slice(xpix[okx].min(), xpix[okx].max()+1)
        
            data = self.model*0.
            data[oky,:][:,okx] += full_array[sly, slx]

        return data
    
    def twod_axis_labels(self, wscale=1.e4, limits=None, mpl_axis=None):
        """Set 2D wavelength (x) axis labels based on spectral parameters
        Parameters
        ----------
        wscale: float
            Scale factor to divide from the wavelength units.  The default 
            value of 1.e4 results in wavelength ticks in microns.
        
        limits: None or list [x0, x1, dx]
            Will automatically use the whole wavelength range defined by the
            spectrum. To change, specify `limits = [x0, x1, dx]` to
            interpolate self.wave between x0*wscale and x1*wscale.

        mpl_axis: matplotlib.axes._subplots.AxesSubplot
            Plotting axis to place the labels.  
        
        Returns
        -------
        Nothing if `mpl_axis` is supplied else pixels and wavelengths of the 
        tick marks.
        """
        xarr = np.arange(len(self.lam))
        if limits:
            xlam = np.arange(limits[0], limits[1], limits[2])
            xpix = np.interp(xlam, self.lam/wscale, xarr)
        else:
            xlam = np.unique(np.cast[int](self.lam / 1.e4*10)/10.)
            xpix = np.interp(xlam, self.lam/wscale, xarr)
        
        if mpl_axis is None:
            return xpix, xlam
        else:
            mpl_axis.set_xticks(xpix)
            mpl_axis.set_xticklabels(xlam)
    
    def twod_xlim(self, x0, x1=None, wscale=1.e4, mpl_axis=None):
        """Set wavelength (x) axis limits on a 2D spectrum
        
        Parameters
        ----------
        x0: float or list/tuple of floats
            minimum or (min,max) of the plot limits
        
        x1: float or None
            max of the plot limits if x0 is a float
            
        wscale: float
            Scale factor to divide from the wavelength units.  The default 
            value of 1.e4 results in wavelength ticks in microns.

        mpl_axis: matplotlib.axes._subplots.AxesSubplot
            Plotting axis to place the labels.  

        Returns
        -------
        Nothing if `mpl_axis` is supplied else pixels the desired wavelength 
        limits.
        """
        if isinstance(x0, list) | isinstance(x0, tuple):
            x0, x1 = x0[0], x0[1]
        
        xarr = np.arange(len(self.lam))
        xpix = np.interp([x0,x1], self.lam/wscale, xarr)
        
        if mpl_axis:
            mpl_axis.set_xlim(xpix)
        else:
            return xpix
    
class ImageData(object):
    """Container for image data with WCS, etc."""
    def __init__(self, sci=np.zeros((1014,1014)), err=None, dq=None,
                 header=None, wcs=None, photflam=1., photplam=1.,
                 origin=[0,0], pad=0,
                 instrument='WFC3', filter='G141', hdulist=None, sci_extn=1):
        """
        Parameters
        ----------
        sci: ndarray
            Science data
        
        err, dq: ndarray or None
            Uncertainty and DQ data.  Defaults to zero if None
            
        header: astropy.io.fits.Header
            Associated header with `data` that contains WCS information
        
        wcs: TBD
        
        photflam: float
            Multiplicative conversion factor to scale `data` to set units 
            to f_lambda flux density.  If data is grism spectra, then use
            photflam=1
        
        origin: [int,int]
            Origin of lower left pixel in detector coordinates
        
        hdulist: astropy.io.HDUList, optional
            If specified, read `sci`, `err`, `dq` from the HDU list from a 
            FITS file, e.g., WFC3 FLT.
        
        sci_extn: int
            Science EXTNAME to read from the HDUList, for example, 
            `sci` = hdulist['SCI',`sci_extn`].
        """
                
        ### Easy way, get everything from an image HDU list
        if isinstance(hdulist, pyfits.HDUList):
            sci = np.cast[np.float32](hdulist['SCI',sci_extn].data)
            err = np.cast[np.float32](hdulist['ERR',sci_extn].data)
            dq = np.cast[np.int16](hdulist['DQ',sci_extn].data)
            
            if 'ORIGINX' in hdulist['SCI', sci_extn].header:
                h0 = hdulist['SCI', sci_extn].header
                origin = [h0['ORIGINY'], h0['ORIGINX']]
            else:
                origin = [0,0]
            
            self.sci_extn = sci_extn    
            self.parent_file = hdulist.filename()
            
            header = hdulist['SCI',sci_extn].header.copy()
            
            status = False
            for ext in [0, ('SCI',sci_extn)]:
                h = hdulist[ext].header
                if 'INSTRUME' in h:
                    status = True
                    break
            
            if not status:
                msg = ('Couldn\'t find \'INSTRUME\' keyword in the headers' + 
                       ' of extensions 0 or (SCI,%d)' %(sci_extn))
                raise KeyError (msg)
            
            instrument = h['INSTRUME']
            filter = utils.get_hst_filter(h)
            if 'PHOTFLAM' in h:
                photflam = h['PHOTFLAM']
            else:
                photflam = photflam_list[filter]
            
            if 'PHOTPLAM' in h:
                photplam = h['PHOTPLAM']
            else:
                photplam = photplam_list[filter]
                        
            if filter.startswith('G'):
                photflam = 1
            
            if 'PAD' in header:
                pad = header['PAD']
            
            self.grow = 1
            if 'GROW' in header:
                self.grow = header['GROW']
            
        else:
            self.parent_file = 'Unknown'
            self.sci_extn = None    
            self.grow = 1
                    
        self.is_slice = False
        
        ### Array parameters
        self.pad = pad
        self.origin = origin
        
        self.data = collections.OrderedDict()
        self.data['SCI'] = sci*photflam

        self.sh = np.array(self.data['SCI'].shape)
        
        ### Header-like parameters
        self.filter = filter
        self.instrument = instrument
        self.header = header
        
        self.photflam = photflam
        self.photplam = photplam
        self.ABZP =  (0*np.log10(self.photflam) - 21.10 -
                      5*np.log10(self.photplam) + 18.6921)
        self.thumb_extension = 'SCI'
          
        if err is None:
            self.data['ERR'] = np.zeros_like(self.data['SCI'])
        else:
            self.data['ERR'] = err*photflam
            if self.data['ERR'].shape != tuple(self.sh):
                raise ValueError ('err and sci arrays have different shapes!')
        
        if dq is None:
            self.data['DQ'] = np.zeros_like(self.data['SCI'], dtype=np.int16)
        else:
            self.data['DQ'] = dq
            if self.data['DQ'].shape != tuple(self.sh):
                raise ValueError ('err and dq arrays have different shapes!')
        
        self.ref_file = None
        self.ref_photflam = None
        self.ref_photplam = None
        self.ref_filter = None
        self.data['REF'] = None
                
        self.wcs = None
        if self.header is not None:
            if wcs is None:
                self.get_wcs()
            else:
                self.wcs = wcs.copy()
        else:
            self.header = pyfits.Header()
            
    def unset_dq(self):
        """Flip OK data quality bits using utils.unset_dq_bits
        
        OK bits are defined as 
        
            okbits_instrument = {'WFC3': 32+64+512, # blob OK
                                 'NIRISS': 0,
                                 'WFIRST': 0,}
        """
                             
        okbits_instrument = {'WFC3': 32+64+512, # blob OK
                             'NIRISS': 0,
                             'WFIRST': 0,}
        
        if self.instrument not in okbits_instrument:
            okbits = 1
        else:
            okbits = okbits_instrument[self.instrument]
                
        self.data['DQ'] = utils.unset_dq_bits(self.data['DQ'], okbits=okbits)
        
    def flag_negative(self, sigma=-3):
        """Flag negative data values with dq=4
        
        Parameters
        ----------
        sigma: float
            Threshold for setting bad data
        
        Returns
        -------
        n_negative: int
            Number of flagged negative pixels
            
        If `self.data['ERR']` is zeros, do nothing.
        """
        if self.data['ERR'].max() == 0:
            return 0
        
        bad = self.data['SCI'] < sigma*self.data['ERR']
        self.data['DQ'][bad] |= 4
        return bad.sum()
        
    def get_wcs(self):
        """Get WCS from header"""
        import numpy.linalg
        
        self.wcs = pywcs.WCS(self.header, relax=True)
        if not hasattr(self.wcs, 'pscale'):
            # self.wcs.pscale = np.sqrt(self.wcs.wcs.cd[0,0]**2 +
            #                           self.wcs.wcs.cd[1,0]**2)*3600.
            
            ### From stwcs.distortion.utils
            det = np.linalg.det(self.wcs.wcs.cd)
            self.wcs.pscale = np.sqrt(np.abs(det))*3600.
            
            #print '%s, PSCALE: %.4f' %(self.parent_file, self.wcs.pscale)
            
    def add_padding(self, pad=200):
        """Pad the data array and update WCS keywords"""
        
        ### Update data array
        new_sh = self.sh + 2*pad
        for key in ['SCI', 'ERR', 'DQ', 'REF']:
            if key not in self.data:
                continue
            else:
                if self.data[key] is None:
                    continue
                    
            data = self.data[key]
            new_data = np.zeros(new_sh, dtype=data.dtype)
            new_data[pad:-pad, pad:-pad] += data
            self.data[key] = new_data
            
        # new_sci = np.zeros(new_sh, dtype=self.sci.dtype)
        # new_sci[pad:-pad,pad:-pad] = self.sci
        # self.sci = new_sci
        
        self.sh = new_sh
        self.pad += pad
        
        ### Padded image dimensions
        self.header['NAXIS1'] += 2*pad
        self.header['NAXIS2'] += 2*pad
        self.wcs.naxis1 = self.wcs._naxis1 = self.header['NAXIS1']
        self.wcs.naxis2 = self.wcs._naxis2 = self.header['NAXIS2']

        ### Add padding to WCS
        self.header['CRPIX1'] += pad
        self.header['CRPIX2'] += pad        
        self.wcs.wcs.crpix[0] += pad
        self.wcs.wcs.crpix[1] += pad
        if self.wcs.sip is not None:
            self.wcs.sip.crpix[0] += pad
            self.wcs.sip.crpix[1] += pad           
    
    def shrink_large_hdu(self, hdu=None, extra=100, verbose=False):
        """Shrink large image mosaic to speed up blotting
        
        Parameters
        ----------
        hdu: astropy.io.ImageHDU
            Input reference HDU
        
        extra: int
            Extra border to put around `self.data` WCS to ensure the reference
            image is large enough to encompass the distorted image
            
        Returns
        -------
        new_hdu: astropy.io.ImageHDU
            Image clipped to encompass `self.data['SCI']` + margin of `extra`
            pixels.
        
        Make a cutout of the larger reference image around the desired FLT
        image to make blotting faster for large reference images.
        """
        ref_wcs = pywcs.WCS(hdu.header)
        
        ### Borders of the flt frame
        naxis = [self.header['NAXIS1'], self.header['NAXIS2']]
        xflt = [-extra, naxis[0]+extra, naxis[0]+extra, -extra]
        yflt = [-extra, -extra, naxis[1]+extra, naxis[1]+extra]
                
        raflt, deflt = self.wcs.all_pix2world(xflt, yflt, 0)
        xref, yref = np.cast[int](ref_wcs.all_world2pix(raflt, deflt, 0))
        ref_naxis = [hdu.header['NAXIS1'], hdu.header['NAXIS2']]
        
        ### Slices of the reference image
        xmi = np.maximum(0, xref.min())
        xma = np.minimum(ref_naxis[0], xref.max())
        slx = slice(xmi, xma)
        
        ymi = np.maximum(0, yref.min())
        yma = np.minimum(ref_naxis[1], yref.max())
        sly = slice(ymi, yma)
        
        if ((xref.min() < 0) | (yref.min() < 0) | 
            (xref.max() > ref_naxis[0]) | (yref.max() > ref_naxis[1])):
            if verbose:
                print ('Image cutout: x=%s, y=%s [Out of range]' 
                    %(slx, sly))
            return hdu
        else:
            if verbose:
                print 'Image cutout: x=%s, y=%s' %(slx, sly)
        
        ### Sliced subimage
        slice_wcs = ref_wcs.slice((sly, slx))
        slice_header = hdu.header.copy()
        hwcs = slice_wcs.to_header(relax=True)

        for k in hwcs.keys():
           if not k.startswith('PC'):
               slice_header[k] = hwcs[k]
                
        slice_data = hdu.data[sly, slx]*1
        new_hdu = pyfits.ImageHDU(data=slice_data, header=slice_header)
        
        return new_hdu
                    
    def blot_from_hdu(self, hdu=None, segmentation=False, grow=3, 
                      interp='nearest'):
        """Blot a rectified reference image to detector frame
        
        Parameters
        ----------
        hdu: astropy.io.ImageHDU
            HDU of the reference image
        
        segmentation: bool, False
            If True, treat the reference image as a segmentation image and 
            preserve the integer values in the blotting.
        
        grow: int, default=3
            Number of pixels to dilate the segmentation regions
            
        interp: str
            Form of interpolation to use when blotting float image pixels. 
            Valid options::
            "nearest", "linear", "poly3", "poly5" (default), "spline3", "sinc"
                    
        Returns
        -------
        blotted: ndarray
            Blotted array with the same shape and WCS as `self.data['SCI']`.
        """
        
        import astropy.wcs
        from drizzlepac import astrodrizzle
        
        #ref = pyfits.open(refimage)
        if hdu.data.dtype.type != np.float32:
            hdu.data = np.cast[np.float32](hdu.data)
        
        refdata = hdu.data
        if 'ORIENTAT' in hdu.header.keys():
            hdu.header.remove('ORIENTAT')
            
        if segmentation:
            seg_ones = np.cast[np.float32](refdata > 0)-1
        
        ref_wcs = pywcs.WCS(hdu.header)        
        flt_wcs = self.wcs.copy()
        
        ### Fix some wcs attributes that might not be set correctly
        for wcs in [ref_wcs, flt_wcs]:
            if (not hasattr(wcs.wcs, 'cd')) & hasattr(wcs.wcs, 'pc'):
                wcs.wcs.cd = wcs.wcs.pc
                
            if hasattr(wcs, 'idcscale'):
                if wcs.idcscale is None:
                    wcs.idcscale = np.mean(np.sqrt(np.sum(wcs.wcs.cd**2, axis=0))*3600.) #np.sqrt(np.sum(wcs.wcs.cd[0,:]**2))*3600.
            else:
                #wcs.idcscale = np.sqrt(np.sum(wcs.wcs.cd[0,:]**2))*3600.
                wcs.idcscale = np.mean(np.sqrt(np.sum(wcs.wcs.cd**2, axis=0))*3600.) #np.sqrt(np.sum(wcs.wcs.cd[0,:]**2))*3600.
            
            wcs.pscale = np.sqrt(wcs.wcs.cd[0,0]**2 +
                                 wcs.wcs.cd[1,0]**2)*3600.
        
        if segmentation:
            ### Handle segmentation images a bit differently to preserve
            ### integers.
            ### +1 here is a hack for some memory issues
            blotted_seg = astrodrizzle.ablot.do_blot(refdata+0., ref_wcs,
                                flt_wcs, 1, coeffs=True, interp='nearest',
                                sinscl=1.0, stepsize=10, wcsmap=None)
            
            blotted_ones = astrodrizzle.ablot.do_blot(seg_ones+1, ref_wcs,
                                flt_wcs, 1, coeffs=True, interp='nearest',
                                sinscl=1.0, stepsize=10, wcsmap=None)
            
            blotted_ones[blotted_ones == 0] = 1
            ratio = np.round(blotted_seg/blotted_ones)
            seg = nd.maximum_filter(ratio, size=grow, mode='constant', cval=0)
            ratio[ratio == 0] = seg[ratio == 0]
            blotted = ratio
            
        else:
            ### Floating point data
            blotted = astrodrizzle.ablot.do_blot(refdata, ref_wcs, flt_wcs, 1,
                                coeffs=True, interp=interp, sinscl=1.0,
                                stepsize=10, wcsmap=None)
        
        return blotted
    
    def get_slice(self, slx=slice(480,520), sly=slice(480,520), 
                 get_slice_header=True):
        """Return cutout version of `self`
        
        Parameters
        ----------
        origin: [int, int]
            Lower left pixel of the slice to cut out
        
        N: int
            Size of the (square) cutout, in pixels
        
        TBD xxx
        slx, sly
        get_wcs
        
        Returns
        -------
        slice_obj: ImageData
            New `ImageData` object of the subregion
        
        slx, sly: 
            Slice parameters of the parent array used to cut out the data
        """
        
        origin = [sly.start, slx.start]
        NX = slx.stop - slx.start
        NY = sly.stop - sly.start
        
        ### Test dimensions
        if (origin[0] < 0) | (origin[0]+NY > self.sh[0]):
            raise ValueError ('Out of range in y')
        
        if (origin[1] < 0) | (origin[1]+NX > self.sh[1]):
            raise ValueError ('Out of range in x')
        
        ### Sliced subimage
        # sly = slice(origin[0], origin[0]+N)
        # slx = slice(origin[1], origin[1]+N)
        
        slice_origin = [self.origin[i] + origin[i] for i in range(2)]
        
        slice_wcs = self.wcs.slice((sly, slx))
        slice_wcs.naxis1 = slice_wcs._naxis1 = NX
        slice_wcs.naxis2 = slice_wcs._naxis2 = NY
        
        ### Getting the full header can be slow as there appears to 
        ### be substantial overhead with header.copy() and wcs.to_header()
        if get_slice_header:
            slice_header = self.header.copy()
            slice_header['NAXIS1'] = NX
            slice_header['NAXIS2'] = NY
        
            ### Sliced WCS keywords
            hwcs = slice_wcs.to_header(relax=True)
            for k in hwcs:
                if not k.startswith('PC'):
                    slice_header[k] = hwcs[k]
                else:
                    cd = k.replace('PC','CD')
                    slice_header[cd] = hwcs[k]
        else:
            slice_header = pyfits.Header()
            
        ### Generate new object        
        slice_obj = ImageData(sci=self.data['SCI'][sly, slx]/self.photflam, 
                              err=self.data['ERR'][sly, slx]/self.photflam, 
                              dq=self.data['DQ'][sly, slx]*1, 
                              header=slice_header, wcs=slice_wcs,
                              photflam=self.photflam, photplam=self.photplam,
                              origin=slice_origin, instrument=self.instrument,
                              filter=self.filter)
        
        slice_obj.ref_photflam = self.ref_photflam
        slice_obj.ref_photplam = self.ref_photplam
        slice_obj.ABZP = self.ABZP
        slice_obj.thumb_extension = self.thumb_extension
        
        if self.data['REF'] is not None:
            slice_obj.data['REF'] = self.data['REF'][sly, slx]*1
        else:
            slice_obj.data['REF'] = None
        
        slice_obj.grow = self.grow
        slice_obj.pad = self.pad             
        slice_obj.parent_file = self.parent_file
        slice_obj.ref_file = self.ref_file
        slice_obj.sci_extn = self.sci_extn
        slice_obj.is_slice = True
        
        return slice_obj#, slx, sly
    
    def get_HDUList(self, extver=1):
        """Convert attributes and data arrays to a `astropy.io.fits.HDUList`
        
        Parameters
        ----------
        extver: int, float, str
            value to use for the 'EXTVER' header keyword.  For example, with 
            extver=1, the science extension can be addressed with the index
            `HDU['SCI',1]`.
        
        returns: astropy.io.fits.HDUList
            HDUList with header keywords copied from `self.header` along with
            keywords for additional attributes. Will have `ImageHDU`
            extensions 'SCI', 'ERR', and 'DQ', as well as 'REF' if a reference
            file had been supplied.
        """
        h = self.header.copy()
        h['EXTVER'] = extver #self.filter #extver
        h['FILTER'] = self.filter, 'element selected from filter wheel'
        h['INSTRUME'] = (self.instrument, 
                         'identifier for instrument used to acquire data')
                         
        h['PHOTFLAM'] = (self.photflam,
                         'inverse sensitivity, ergs/cm2/Ang/electron')
                                        
        h['PHOTPLAM'] = self.photplam, 'Pivot wavelength (Angstroms)'
        h['PARENT'] = self.parent_file, 'Parent filename'
        h['SCI_EXTN'] = self.sci_extn, 'EXTNAME of the science data'
        h['ISCUTOUT'] = self.is_slice, 'Arrays are sliced from larger image'
        h['ORIGINX'] = self.origin[1], 'Origin from parent image, x'
        h['ORIGINY'] = self.origin[0], 'Origin from parent image, y'
        
        hdu = []
        
        hdu.append(pyfits.ImageHDU(data=self.data['SCI'], header=h,
                                   name='SCI'))
        hdu.append(pyfits.ImageHDU(data=self.data['ERR'], header=h,
                                   name='ERR'))
        hdu.append(pyfits.ImageHDU(data=self.data['DQ'], header=h, name='DQ'))
        
        if self.data['REF'] is not None:
            h['PHOTFLAM'] = self.ref_photflam
            h['PHOTPLAM'] = self.ref_photplam
            h['FILTER'] = self.ref_filter
            h['REF_FILE'] = self.ref_file
            
            hdu.append(pyfits.ImageHDU(data=self.data['REF'], header=h,
                                       name='REF'))
            
        hdul = pyfits.HDUList(hdu)
        
        return hdul
                             
    def __getitem__(self, ext):
        if self.data[ext] is None:
            return None
        
        if ext == 'REF':
            return self.data['REF']/self.ref_photflam
        elif ext == 'DQ':
            return self.data['DQ']
        else:
            return self.data[ext]/self.photflam
            
class GrismFLT(object):
    """Scripts for modeling of individual grism FLT images"""
    def __init__(self, grism_file='', sci_extn=1, direct_file='',
                 pad=200, ref_file=None, ref_ext=0, seg_file=None,
                 shrink_segimage=True, force_grism='G141', verbose=True):
        """Read FLT files and, optionally, reference/segmentation images.
        
        Parameters
        ----------
        grism_file: str
            Grism image (optional).
            Empty string or filename of a FITS file that must contain
            extensions ('SCI', `sci_extn`), ('ERR', `sci_extn`), and
            ('DQ', `sci_extn`).  For example, a WFC3/IR "FLT" FITS file.
            
        sci_extn: int
            EXTNAME of the file to consider.  For WFC3/IR this can only be 
            1.  For ACS and WFC3/UVIS, this can be 1 or 2 to specify the two
            chips.
            
        direct_file: str
            Direct image (optional).
            Empty string or filename of a FITS file that must contain
            extensions ('SCI', `sci_extn`), ('ERR', `sci_extn`), and
            ('DQ', `sci_extn`).  For example, a WFC3/IR "FLT" FITS file.
        
        pad: int
            Padding to add around the periphery of the images to allow 
            modeling of dispersed spectra for objects that could otherwise 
            fall off of the direct image itself.  Modeling them requires an 
            external reference image (`ref_file`) that covers an area larger
            than the individual direct image itself (e.g., a mosaic of a 
            survey field).
        
            For WFC3/IR spectra, the first order spectra reach 248 and 195 
            pixels for G102 and G141, respectively, and `pad` could be set
            accordingly if the reference image is large enough.
        
        ref_file: str or ImageHDU/PrimaryHDU
            Image mosaic to use as the reference image in place of the direct
            image itself.  For example, this could be the deeper image
            drizzled from all direct images taken within a single visit or it
            could be a much deeper/wider image taken separately in perhaps 
            even a different filter.
            
            N.B. Assumes that the WCS are aligned between `grism_file`, 
            `direct_file` and `ref_file`!
            
        ref_ext: int
            FITS extension to use if `ref_file` is a filename string.
        
        seg_file: str or ImageHDU/PrimaryHDU
            Segmentation image mosaic to associate pixels with discrete 
            objects.  This would typically be generated from a rectified 
            image like `ref_file`, though here it is not required that 
            `ref_file` and `seg_file` have the same image dimensions but 
            rather just that the WCS are aligned between them.
        
        shrink_segimage: bool
            Try to make a smaller cutout of the reference images to speed 
            up blotting and array copying.  This is most helpful for very 
            large input mosaics.
        
        force_grism: str
            Use this grism in "simulation mode" where only `direct_file` is
            specified.
            
        verbose: bool
            Print status messages to the terminal.
            
        """
        
        ### Read files
        self.grism_file = grism_file
        if os.path.exists(grism_file):
            grism_im = pyfits.open(grism_file)
            self.grism = ImageData(hdulist=grism_im, sci_extn=sci_extn)
        else:
            self.grism = None
            
        self.direct_file = direct_file
        if os.path.exists(direct_file):
            direct_im = pyfits.open(direct_file)
            self.direct = ImageData(hdulist=direct_im, sci_extn=sci_extn)
        else:
            self.direct = None
        
        # ### Simulation mode, no grism exposure
        self.pad = self.grism.pad
        
        if (self.grism is None) & (self.direct is not None):
            self.grism = ImageData(hdulist=direct_im, sci_extn=sci_extn)
            self.grism_file = self.direct_file
            self.grism.filter = force_grism
            
        ### Grism exposure only, assumes will get reference from ref_file
        if (self.direct is None) & (self.grism is not None):
            self.direct = ImageData(hdulist=grism_im, sci_extn=sci_extn)
            self.direct_file = self.grism_file
            
        ### Add padding
        if self.direct is not None:
            if pad > 0:
                self.direct.add_padding(pad)
            
            self.direct.unset_dq()            
            nbad = self.direct.flag_negative(sigma=-3)
            self.direct.data['SCI'] *= (self.direct.data['DQ'] == 0)
            
        if self.grism is not None:
            if pad > 0:
                self.grism.add_padding(pad)
                self.pad = self.grism.pad
                
            self.grism.unset_dq()
            nbad = self.grism.flag_negative(sigma=-3)
            self.grism.data['SCI'] *= (self.grism.data['DQ'] == 0)
                
        ### Load data from saved model files, if available
        # if os.path.exists('%s_model.fits' %(self.grism_file)):
        #     pass
            
        ### Holder for the full grism model array
        self.model = np.zeros_like(self.direct.data['SCI'])
        
        ### Grism configuration
        self.conf_file = grismconf.get_config_filename(self.grism.instrument,
                                                       self.direct.filter,
                                                       self.grism.filter)
        
        self.conf = grismconf.load_grism_config(self.conf_file)
        
        self.object_dispersers = collections.OrderedDict()
                    
        ### Blot reference image
        self.process_ref_file(ref_file, ref_ext=ref_ext, 
                              shrink_segimage=shrink_segimage,
                              verbose=verbose)
        
        ### Blot segmentation image
        self.process_seg_file(seg_file, shrink_segimage=shrink_segimage,
                              verbose=verbose)
        
        ## End things
        self.get_dispersion_PA()
        
        self.catalog = None
                            
        
    def process_ref_file(self, ref_file, ref_ext=0, shrink_segimage=True,
                         verbose=True):
        """Read and blot a reference image
        
        Parameters
        ----------
        ref_file: str or ImageHDU/PrimaryHDU
            Filename or `astropy.io.fits` Image HDU of the reference image.
        
        shrink_segimage: bool
            Try to make a smaller cutout of the reference image to speed 
            up blotting and array copying.  This is most helpful for very 
            large input mosaics.
        
        verbose: bool
            Print some status information to the terminal
        
        Returns
        -------
        status: bool
            False if `ref_file` is None.  True if completes successfully.
            
        The blotted reference image is stored in the array attribute
        `self.direct.data['REF']`.
        
        The `ref_filter` attribute is determined from the image header and the
        `ref_photflam` scaling is taken either from the header if possible, or
        the global `photflam` variable defined at the top of this file.
        """
        if ref_file is None:
            return False
            
        if (isinstance(ref_file, pyfits.ImageHDU) | 
            isinstance(ref_file, pyfits.PrimaryHDU)):
            self.ref_file = ref_file.fileinfo()['file'].name
            ref_str = ''
            ref_hdu = ref_file
            refh = ref_hdu.header
        else:
            self.ref_file = ref_file
            ref_str = '%s[0]' %(self.ref_file)
            ref_hdu = pyfits.open(ref_file)[ref_ext]
            refh = ref_hdu.header
        
        if shrink_segimage:
            ref_hdu = self.direct.shrink_large_hdu(ref_hdu, extra=self.pad,
                                                   verbose=True)
            
        if verbose:
            print '%s / blot reference %s' %(self.direct_file, ref_str)
                                              
        blotted_ref = self.grism.blot_from_hdu(hdu=ref_hdu,
                                      segmentation=False, interp='poly5')
        
        if 'PHOTFLAM' in refh:
            self.direct.ref_photflam = ref_hdu.header['PHOTFLAM']
        else:
            self.direct.ref_photflam = photflam_list[refh['FILTER'].upper()]
        
        if 'PHOTPLAM' in refh:
            self.direct.ref_photplam = ref_hdu.header['PHOTPLAM']
        else:
            self.direct.ref_photplam = photplam_list[refh['FILTER'].upper()]
        
        ## TBD: compute something like a cross-correlation offset
        ##      between blotted reference and the direct image itself
        self.direct.data['REF'] = np.cast[np.float32](blotted_ref)
        self.direct.data['REF'] *= self.direct.ref_photflam
        
        ## Fill empty pixels in the reference image from the SCI image
        empty = self.direct.data['REF'] == 0
        self.direct.data['REF'][empty] += self.direct.data['SCI'][empty]
        
        # self.direct.data['ERR'] *= 0.
        # self.direct.data['DQ'] *= 0
        self.direct.ref_filter = utils.get_hst_filter(refh)
        self.direct.ref_file = ref_str
        
        self.ABZP =  (0*np.log10(self.direct.ref_photflam) - 21.10 -
                      5*np.log10(self.direct.ref_photplam) + 18.6921)
        
        self.thumb_extension = 'REF'
        
        #refh['FILTER'].upper()
        return True
        
    def process_seg_file(self, seg_file, shrink_segimage=True, verbose=True):
        """Read and blot a rectified segmentation image
        
        Parameters
        ----------
        seg_file: str or ImageHDU/PrimaryHDU
            Filename or `astropy.io.fits` Image HDU of the segmentation image.
        
        shrink_segimage: bool
            Try to make a smaller cutout of the segmentation image to speed 
            up blotting and array copying.  This is most helpful for very 
            large input mosaics.
        
        verbose: bool
            Print some status information to the terminal
        
        Returns
        -------
        The blotted segmentation image is stored in `self.seg`.
        
        """
        if seg_file is not None:
            if (isinstance(seg_file, pyfits.ImageHDU) | 
                isinstance(seg_file, pyfits.PrimaryHDU)):
                self.seg_file = ''
                seg_str = ''
                seg_hdu = seg_file
                segh = seg_hdu.header
            else:
                self.seg_file = seg_file
                seg_str = '%s[0]' %(self.seg_file)
                seg_hdu = pyfits.open(seg_file)[0]
                segh = seg_hdu.header
            
            if shrink_segimage:
                seg_hdu = self.direct.shrink_large_hdu(seg_hdu, 
                                                       extra=self.pad,
                                                       verbose=True)
                
            
            if verbose:
                print '%s / blot segmentation %s' %(self.direct_file, seg_str)
            
            blotted_seg = self.grism.blot_from_hdu(hdu=seg_hdu,
                                          segmentation=True, grow=3)
            self.seg = blotted_seg
                        
        else:
            self.seg = np.zeros(self.direct.sh, dtype=np.float32)
    
    def get_dispersion_PA(self):
        """
        Compute exact PA of the dispersion axis, including tilt of the 
        trace and the FLT WCS
        """
        from astropy.coordinates import Angle
        import astropy.units as u
                    
        ### extra tilt of the 1st order grism spectra
        x0 =  self.conf.conf['BEAMA']
        dy_trace, lam_trace = self.conf.get_beam_trace(x=507, y=507, dx=x0,
                                                       beam='A')
        
        extra = np.arctan2(dy_trace[1]-dy_trace[0], x0[1]-x0[0])/np.pi*180
                
        ### Distorted WCS
        crpix = self.direct.wcs.wcs.crpix
        xref = [crpix[0], crpix[0]+1]
        yref = [crpix[1], crpix[1]]
        r, d = self.direct.wcs.all_pix2world(xref, yref, 1)
        pa =  Angle((extra + 
                     np.arctan2(np.diff(r), np.diff(d))[0]/np.pi*180)*u.deg)
        
        self.dispersion_PA = pa.wrap_at(360*u.deg).value
        
    def compute_model_orders(self, id=0, x=None, y=None, size=10, mag=-1,
                      spectrum_1d=None, compute_size=False, store=True, 
                      in_place=True, add=True, get_beams=None, verbose=True):
        """Compute dispersed spectrum for a given object id
        
        Parameters
        ----------
        id: int
            Object ID number to match in the segmentation image
        
        x, y: float, float
            Center of the cutout to extract
        
        size: int
            Radius of the cutout to extract.  The cutout is equivalent to 
            
        >>> xc, yc = int(x), int(y)
        >>> thumb = self.direct.data['SCI'][yc-size:yc+size, xc-size:xc+size]
        
        mag: float
            Specified object magnitude, which will be compared to the 
            "MMAG_EXTRACT_[BEAM]" parameters in `self.conf` to decide if the 
            object is bright enough to compute the higher spectral orders.  
            Default of -1 means compute all orders listed in `self.conf.beams`
        
        spectrum_1d: None or [array, array]
            Template 1D spectrum to convolve with the grism disperser.  If 
            None, assumes trivial spectrum flat in f_lambda flux densities.  
            Otherwise, the template is taken to be
            
            >>> wavelength, flux = spectrum_1d
        
        compute_size: bool
            Ignore `x`, `y`, and `size` and compute the extent of the 
            segmentation polygon directly using 
            `utils_c.disperse.compute_segmentation_limits`.
        
        store: bool
            If True, then store the computed beams in the OrderedDict
            `self.object_dispersers[id]`.  
            
            If many objects are computed, this can be memory intensive. To 
            save memory, set to False and then the function just stores the
            input template spectrum (`spectrum_1d`) and the beams will have
            to be recomputed if necessary.
                    
        in_place: bool
            If True, add the computed spectral orders into `self.model`.  
            Otherwise, make a clean array with only the orders of the given
            object.
            
        Returns
        -------
        output: bool or array
            If `in_place` is True, return status of True if everything goes
            OK. The computed spectral orders are stored in place in
            `self.model`.
            
            Returns False if the specified `id` is not found in the
            segmentation array independent of `in_place`.
            
            If `in_place` is False, return a full array including the model 
            for the single object.
        """               
        if id in self.object_dispersers.keys():
            object_in_model = True
            beams = self.object_dispersers[id]
        else:
            object_in_model = False
            beams = None
        
        if self.direct.data['REF'] is None:
            ext = 'SCI'
        else:
            ext = 'REF'
            
        ### Do we need to compute the dispersed beams?
        if not isinstance(beams, collections.OrderedDict):
            ### Use catalog
            xcat = ycat = None
            if self.catalog is not None:
                ix = self.catalog['id'] == id
                if ix.sum() == 0:
                    if verbose:
                        print 'ID %d not found in segmentation image' %(id)
                    return False
                
                xcat = self.catalog['x_flt'][ix][0]-1
                ycat = self.catalog['y_flt'][ix][0]-1
                #print '!!! X, Y: ', xcat, ycat, self.direct.origin, size
                
            if (compute_size) | (x is None) | (y is None) | (size is None):
                ### Get the array indices of the segmentation region
                out = disperse.compute_segmentation_limits(self.seg, id,
                                         self.direct.data[ext],
                                         self.direct.sh)
                
                ymin, ymax, y, xmin, xmax, x, area, segm_flux = out
                if (area == 0) | ~np.isfinite(x) | ~np.isfinite(y):
                    if verbose:
                        print 'ID %d not found in segmentation image' %(id)
                    return False
                
                ### Object won't disperse spectrum onto the grism image
                if ((ymax < self.pad-5) | 
                    (ymin > self.direct.sh[0]-self.pad+5) | 
                    (xmin == 0) | (ymax == self.direct.sh[0]) |
                    (xmin == 0) | (xmax == self.direct.sh[1])):
                    return True
                    
                if compute_size:
                    try:
                        size = int(np.ceil(np.max([x-xmin, xmax-x, 
                                                   y-ymin, ymax-y])))
                    except ValueError:
                        return False
                    
                    size += 4
                
                    ## Enforce minimum size
                    size = np.maximum(size, 16)
                    size = np.maximum(size, 26)
                    ## Avoid problems at the array edges
                    size = np.min([size, int(x)-2, int(y)-2])
                
                    if (size < 4):
                        return True
                    
            ### Thumbnails
            #print '!! X, Y: ', x, y, self.direct.origin, size
            
            if xcat is not None:
                xc, yc = int(np.round(xcat))+1, int(np.round(ycat))+1
                xcenter = -(xcat-(xc-1))
                ycenter = -(ycat-(yc-1))
            else:
                xc, yc = int(np.round(x))+1, int(np.round(y))+1
                xcenter = -(x-(xc-1))
                ycenter = -(y-(yc-1))
                
            origin = [yc-size + self.direct.origin[0], 
                      xc-size + self.direct.origin[1]]
                                      
            thumb = self.direct.data[ext][yc-size:yc+size, xc-size:xc+size]
            seg_thumb = self.seg[yc-size:yc+size, xc-size:xc+size]
            
            ## Test that the id is actually in the thumbnail
            test = disperse.compute_segmentation_limits(seg_thumb, id, thumb,
                                                        np.array(thumb.shape))
            if test[-2] == 0:
                if verbose:
                    print 'ID %d not found in segmentation image' %(id)
                return False
            
            ### Compute spectral orders ("beams")
            beams = collections.OrderedDict()
            if get_beams is None:
                beam_names = self.conf.beams
            else:
                beam_names = get_beams
                
            for beam in beam_names:
                ### Only compute order if bright enough
                if mag > self.conf.conf['MMAG_EXTRACT_%s' %(beam)]:
                    continue
                    
                try:
                    b = GrismDisperser(id=id, direct=thumb, segmentation=seg_thumb, xcenter=xcenter, ycenter=ycenter, origin=origin, pad=self.pad, grow=self.grism.grow,beam=beam, conf=self.conf)
                except:
                    continue
                
                beams[beam] = b
                if object_in_model:
                    #old_spectrum_1d = beams
                    old_spectrum_1d = self.object_dispersers[id]
                    b.compute_model(id=id, spectrum_1d=old_spectrum_1d)
            
            if get_beams:
                return beams
                
            if in_place:
                if store:
                    ### Save the computed beams 
                    self.object_dispersers[id] = beams
                else:
                    ### Just save the model spectrum (or empty spectrum)
                    self.object_dispersers[id] = spectrum_1d
                        
        if in_place:
            ### Update the internal model attribute
            output = self.model
        else:
            ### Create a fresh array
            output = np.zeros_like(self.model)
                
        ### Loop through orders and add to the full model array, in-place or
        ### a separate image 
        for b in beams.keys():
            beam = beams[b]
            
            ### Subtract previously-added model
            if object_in_model & in_place:
                beam.add_to_full_image(-beam.model, output)
            
            ### Add in new model
            beam.compute_model(id=id, spectrum_1d=spectrum_1d)
            beam.add_to_full_image(beam.model, output)
        
        if in_place:
            return True
        else:
            return beams, output
    
    def compute_full_model(self, ids=None, mags=None, refine_maglim=22,
                           store=True, verbose=False):
        """Compute flat-spectrum model for multiple objects.
        
        Parameters
        ----------
        ids: None, list, or array
            id numbers to compute in the model.  If None then take all ids 
            from unique values in `self.seg`.
        
        mags: None, float, or list/array
            magnitudes corresponding to list if `ids`.  If None, then compute
            magnitudes based on the flux in segmentation regions and 
            zeropoints determined from PHOTFLAM and PHOTPLAM.
        
        Returns
        -------
        Updated model stored in `self.model`.
        """
        if ids is None:
            ids = np.unique(self.seg)[1:]
        
        ### If `mags` array not specified, compute magnitudes within
        ### segmentation regions.
        if mags is None:                                
            mags = np.zeros(len(ids))
            for i, id in enumerate(ids):
                out = disperse.compute_segmentation_limits(self.seg, id,
                                     self.direct.data[self.thumb_extension],
                                     self.direct.sh)
            
                ymin, ymax, y, xmin, xmax, x, area, segm_flux = out
                mags[i] = self.ABZP - 2.5*np.log10(segm_flux)
        else:
            if np.isscalar(mags):
                mags = [mags for i in range(len(ids))]
            else:
                if len(ids) != len(mags):
                    raise ValueError ('`ids` and `mags` lists different sizes')
        
        ### Now compute the full model
        for id_i, mag_i in zip(ids, mags):
            if verbose:
                print utils.no_newline + 'compute model id=%d' %(id_i)
                
            self.compute_model_orders(id=id_i, compute_size=True, mag=mag_i, 
                                      in_place=True, store=store)
    
    def smooth_mask(self, gaussian_width=4, threshold=2.5):
        """TBD
        """
        import scipy.ndimage as nd
        
        mask = self.grism['SCI'] != 0
        resid = (self.grism['SCI'] - self.model)*mask
        sm = nd.gaussian_filter(np.abs(resid), gaussian_width)
        resid_mask = (np.abs(sm) > threshold*self.grism['ERR'])
        self.grism.data['SCI'][resid_mask] = 0
        
    def blot_catalog(self, input_catalog, columns=['id','ra','dec'], 
                     sextractor=False, ds9=None):
        """Compute detector-frame coordinates of sky positions in a catalog.
        
        Parameters
        ----------
        input_catalog: astropy.table.Table
            Full catalog with sky coordinates.  Can be SExtractor or other.
        
        columns: [str,str,str]
            List of columns that specify the object id, R.A. and Decl.  For 
            catalogs created with SExtractor this might be 
            ['NUMBER', 'X_WORLD', 'Y_WORLD'].
            
            Detector coordinates will be computed with 
            `self.direct.wcs.all_world2pix` with origin=1.
        
        ds9: pyds9.DS9, optional
            If provided, load circular regions at the derived detector
            coordinates.
        
        Returns
        -------
        catalog: astropy.table.Table
            New catalog with columns 'x_flt' and 'y_flt' of the detector 
            coordinates.  Also will copy the `columns` names to columns with 
            names 'id','ra', and 'dec' if necessary, e.g., for SExtractor 
            catalogs.
            
        """
        from astropy.table import Column
        
        if sextractor:
            columns = ['NUMBER', 'X_WORLD', 'Y_WORLD']
            
        ### Detector coordinates.  N.B.: 1 indexed!
        xy = self.direct.wcs.all_world2pix(input_catalog[columns[1]], 
                                           input_catalog[columns[2]], 1,
                                           tolerance=-4,
                                           quiet=True)
        
        ### Objects with positions within the image
        sh = self.direct.sh
        keep = ((xy[0] > 0) & (xy[0] < sh[1]) & 
                (xy[1] > (self.pad-5)) & (xy[1] < (sh[0]-self.pad+5)))
                
        catalog = input_catalog[keep]
        
        ### Remove columns if they exist
        for col in ['x_flt', 'y_flt']:
            if col in catalog.colnames:
                catalog.remove_column(col)
                
        ### Columns with detector coordinates
        catalog.add_column(Column(name='x_flt', data=xy[0][keep]))
        catalog.add_column(Column(name='y_flt', data=xy[1][keep]))
        
        ### Copy standardized column names if necessary
        if ('id' not in catalog.colnames):
            catalog.add_column(Column(name='id', data=catalog[columns[0]]))
        
        if ('ra' not in catalog.colnames):
            catalog.add_column(Column(name='ra', data=catalog[columns[1]]))
        
        if ('dec' not in catalog.colnames):
            catalog.add_column(Column(name='dec', data=catalog[columns[2]]))
        
        ### Show positions in ds9
        if ds9:
            for i in range(len(catalog)):
                x_flt, y_flt = catalog['x_flt'][i], catalog['y_flt'][i]
                reg = 'circle %f %f 5\n' %(x_flt, y_flt)
                ds9.set('regions', reg)
        
        return catalog
    
    def photutils_detection(self, use_seg=False, data_ext='SCI',
                            detect_thresh=2., grow_seg=5, gauss_fwhm=2.,
                            verbose=True, save_detection=False, ZP=None):
        """Use photutils to detect objects and make segmentation map
        
        Parameters
        ----------
        detect_thresh: float
            Detection threshold, in sigma
        
        grow_seg: int
            Number of pixels to grow around the perimeter of detected objects
            witha  maximum filter
        
        gauss_fwhm: float
            FWHM of Gaussian convolution kernel that smoothes the detection
            image.
        
        verbose: bool
            Print logging information to the terminal
        
        save_detection: bool
            Save the detection images and catalogs
        
        ZP: float or None
            AB magnitude zeropoint of the science array.  If `None` then, try
            to compute based on PHOTFLAM and PHOTPLAM values and use zero if
            that fails.
            
        Returns
        ---------
        status: bool
            True if completed successfully.  False if `data_ext=='REF'` but 
            no ref image found.
            
        Stores an astropy.table.Table object to `self.catalog` and a 
        segmentation array to `self.seg`.
        
        """
        if ZP is None:
            if ((self.direct.filter in photflam_list.keys()) & 
                (self.direct.filter in photplam_list.keys())):
                ### ABMAG_ZEROPOINT from
                ### http://www.stsci.edu/hst/wfc3/phot_zp_lbn
                ZP =  (-2.5*np.log10(photflam_list[self.direct.filter]) -
                       21.10 - 5*np.log10(photplam_list[self.direct.filter]) +
                       18.6921)
            else:
                ZP = 0.
        
        if use_seg:
            seg = self.seg
        else:
            seg = None
        
        if self.direct.data['ERR'].max() != 0.:
            err = self.direct.data['ERR']/self.direct.photflam
        else:
            err = None
        
        if (data_ext == 'REF'):
            if (self.direct.data['REF'] is not None):
                err = None
            else:
                print 'No reference data found for `self.direct.data[\'REF\']`'
                return False
                
        go_detect = utils.detect_with_photutils    
        cat, seg = go_detect(self.direct.data[data_ext]/self.direct.photflam,
                             err=err, dq=self.direct.data['DQ'], seg=seg,
                             detect_thresh=detect_thresh, npixels=8,
                             grow_seg=grow_seg, gauss_fwhm=gauss_fwhm,
                             gsize=3, wcs=self.direct.wcs,
                             save_detection=save_detection, 
                             root=self.direct_file.split('.fits')[0],
                             background=None, gain=None, AB_zeropoint=ZP,
                             clobber=True, verbose=verbose)
        
        self.catalog = cat
        self.seg = seg
        
        return True
        
    def load_photutils_detection(self, seg_file=None, seg_cat=None, 
                                 catalog_format='ascii.commented_header'):
        """
        Load segmentation image and catalog, either from photutils 
        or SExtractor.  
        
        If SExtractor, use `catalog_format='ascii.sextractor'`.
        
        """
        root = self.direct_file.split('.fits')[0]
        
        if seg_file is None:
            seg_file = root + '.detect_seg.fits'
        
        if not os.path.exists(seg_file):
            print 'Segmentation image %s not found' %(segfile)
            return False
        
        self.seg = np.cast[np.float32](pyfits.open(seg_file)[0].data)
        
        if seg_cat is None:
            seg_cat = root + '.detect.cat'
        
        if not os.path.exists(seg_cat):
            print 'Segmentation catalog %s not found' %(seg_cat)
            return False
        
        self.catalog = Table.read(seg_cat, format=catalog_format)
    
    def save_model(self, clobber=True, verbose=True):
        """Save model properties to FITS file
        """
        import cPickle as pickle
        
        root = self.grism_file.split('_flt.fits')[0]
        
        h = pyfits.Header()
        h['GFILE'] = (self.grism_file, 'Grism exposure name')
        h['GFILTER'] = (self.grism.filter, 'Grism spectral element')
        h['INSTRUME'] = (self.grism.instrument, 'Instrument of grism file')
        h['PAD'] = (self.pad, 'Image padding used')
        h['DFILE'] = (self.direct_file, 'Direct exposure name')
        h['DFILTER'] = (self.direct.filter, 'Grism spectral element')
        h['REF_FILE'] = (self.ref_file, 'Reference image')
        h['SEG_FILE'] = (self.seg_file, 'Segmentation image')
        h['CONFFILE'] = (self.conf_file, 'Configuration file')
        h['DISP_PA'] = (self.dispersion_PA, 'Dispersion position angle')
        
        h0 = pyfits.PrimaryHDU(header=h)
        model = pyfits.ImageHDU(data=self.model, header=self.grism.header, 
                                name='MODEL')
                                
        seg = pyfits.ImageHDU(data=self.seg, header=self.grism.header,
                              name='SEG')

        hdu = pyfits.HDUList([h0, model, seg])
        
        if 'REF' in self.direct.data:
            ref_header = self.grism.header.copy()
            ref_header['FILTER'] = self.direct.ref_filter
            ref_header['PARENT'] = self.ref_file
            ref_header['PHOTFLAM'] = self.direct.ref_photflam
            ref_header['PHOTPLAM'] = self.direct.ref_photplam
            
            ref = pyfits.ImageHDU(data=self.direct['REF'],
                                  header=ref_header, name='REFERENCE')
        
            hdu.append(ref)
            
        hdu.writeto('%s_model.fits' %(root), clobber=clobber,
                    output_verify='fix')
        
        fp = open('%s_model.pkl' %(root), 'wb')
        pickle.dump(self.object_dispersers, fp)
        fp.close()
        
        if verbose:
            print 'Saved %s_model.fits and %s_model.pkl' %(root, root)
    
    def save_full_pickle(self, verbose=True):
        """Save entire `GrismFLT` object to a pickle
        """
        import cPickle as pickle
        
        root = self.grism_file.split('_flt.fits')[0].split('_cmb.fits')[0]

        hdu = pyfits.HDUList([pyfits.PrimaryHDU()])
        for key in self.direct.data.keys():
            hdu.append(pyfits.ImageHDU(data=self.direct.data[key],
                                       header=self.direct.header, 
                                       name='D'+key))
        
        for key in self.grism.data.keys():
            hdu.append(pyfits.ImageHDU(data=self.grism.data[key],
                                       header=self.grism.header, 
                                       name='G'+key))
        
        hdu.append(pyfits.ImageHDU(data=self.seg,
                                   header=self.grism.header, 
                                   name='SEG'))
        
        hdu.append(pyfits.ImageHDU(data=self.model,
                                   header=self.grism.header, 
                                   name='MODEL'))
        
        
        hdu.writeto('%s_GrismFLT.fits' %(root), clobber=True, 
                    output_verify='fix')
        
        ## zero out large data objects
        self.direct.data = self.grism.data = self.seg = self.model = None
                                            
        fp = open('%s_GrismFLT.pkl' %(root), 'wb')
        pickle.dump(self, fp)
        fp.close()
    
    def load_from_fits(self, save_file):
        """Load saved data
        
        TBD
        """
        fits = pyfits.open(save_file)
        self.seg = fits['SEG'].data*1
        self.model = fits['MODEL'].data*1
        self.direct.data = collections.OrderedDict()
        self.grism.data = collections.OrderedDict()
        
        for ext in range(1,len(fits)):
            key = fits[ext].header['EXTNAME'][1:]
            
            if fits[ext].header['EXTNAME'].startswith('D'):
                if fits[ext].data is None:
                    self.direct.data[key] = None
                else:
                    self.direct.data[key] = fits[ext].data*1
            elif fits[ext].header['EXTNAME'].startswith('G'):
                if fits[ext].data is None:
                    self.grism.data[key] = None
                else:
                    self.grism.data[key] = fits[ext].data*1
            else:
                pass
                
        return True
        
class BeamCutout(object):
    def __init__(self, flt=None, beam=None, conf=None, 
                 get_slice_header=True, fits_file=None,
                 contam_sn_mask=[10,3]):
        """Cutout spectral object from the full frame.
        
        Parameters
        ----------
        flt: GrismFLT
            Parent FLT frame.
        
        beam: GrismDisperser
            Object and spectral order to consider
        
        conf: grismconf.aXeConf
            Pre-computed configuration file.  If not specified will regenerate
            based on header parameters, which might be necessary for 
            multiprocessing parallelization and pickling.
        
        get_slice_header: bool
            TBD
            
        fits_file: None or str
            Optional FITS file containing the beam information
            
        contam_sn_mask: TBD
        """
        if fits_file is not None:
            self.load_fits(fits_file, conf)
        else:
            self.init_from_input(flt, beam, conf, get_slice_header)
                    
        ### bad pixels or problems with uncertainties
        self.mask = ((self.grism.data['DQ'] > 0) | 
                     (self.grism.data['ERR'] == 0) | 
                     (self.grism.data['SCI'] == 0))
                             
        self.ivar = 1/self.grism.data['ERR']**2
        self.ivar[self.mask] = 0
                
        #self.compute_model = self.beam.compute_model
        self.model = self.beam.model
        self.modelf = self.model.flatten()
        
        ### Initialize for fits
        self.flat_flam = self.compute_model(in_place=False)
        
        ### OK data where the 2D model has non-zero flux
        self.fit_mask = (~self.mask.flatten()) & (self.ivar.flatten() != 0)
        self.fit_mask &= (self.flat_flam > 0.01*self.flat_flam.max())
        #self.fit_mask &= (self.flat_flam > 3*self.contam.flatten())
            
        ### Flat versions of sci/ivar arrays
        self.scif = (self.grism.data['SCI'] - self.contam).flatten()
        self.ivarf = self.ivar.flatten()
        
        ### Mask large residuals
        resid = np.abs(self.scif - self.flat_flam)*np.sqrt(self.ivarf)
        bad_resid = (self.flat_flam < 0.05*self.flat_flam.max()) & (resid > 5)
        self.fit_mask *= ~bad_resid
        
        ### Mask very contaminated
        contam_mask = ((self.contam*np.sqrt(self.ivar) > contam_sn_mask[0]) & 
                      (self.model*np.sqrt(self.ivar) < contam_sn_mask[1]))
        #self.fit_mask *= ~contam_mask.flatten()
        self.contam_mask = ~nd.maximum_filter(contam_mask, size=5).flatten()
        self.poly_order = None
        #self.init_poly_coeffs(poly_order=1)
        
    def init_from_input(self, flt, beam, conf=None, get_slice_header=True):
        """Initialize from data objects
        
        Parameters
        ----------
        flt: GrismFLT
            Parent FLT frame.
        
        beam: GrismDisperser
            Object and spectral order to consider
        
        conf: grismconf.aXeConf
            Pre-computed configuration file.  If not specified will regenerate
            based on header parameters, which might be necessary for 
            multiprocessing parallelization and pickling.
        
        get_slice_header: TBD
        
        Returns
        -------
        Loads attributes to `self`.
        """
        self.id = beam.id
        if conf is None:
            conf = grismconf.load_grism_config(flt.conf_file)
                    
        self.beam = GrismDisperser(id=beam.id, direct=beam.direct*1,
                           segmentation=beam.seg*1, origin=beam.origin,
                           pad=beam.pad, grow=beam.grow,
                           beam=beam.beam, conf=conf, xcenter=beam.xcenter,
                           ycenter=beam.ycenter)
        
        self.beam.compute_model(spectrum_1d = beam.spectrum_1d)
        
        slx_thumb = slice(self.beam.origin[1], 
                          self.beam.origin[1]+self.beam.sh[1])
                          
        sly_thumb = slice(self.beam.origin[0], 
                          self.beam.origin[0]+self.beam.sh[0])

        self.direct = flt.direct.get_slice(slx_thumb, sly_thumb, 
                                           get_slice_header=get_slice_header)
        self.grism = flt.grism.get_slice(self.beam.slx_parent,
                                         self.beam.sly_parent,
                                         get_slice_header=get_slice_header)
        
        self.contam = flt.model[self.beam.sly_parent, self.beam.slx_parent]*1
        if self.beam.id in flt.object_dispersers:
            self.contam -= self.beam.model
        
    def load_fits(self, file, conf=None):
        """Initialize from FITS file
        
        Parameters
        ----------
        file: str
            FITS file to read (as output from `self.write_fits`).
                
        Returns
        -------
        Loads attributes to `self`.
        """        
        hdu = pyfits.open(file)
        
        self.direct = ImageData(hdulist=hdu, sci_extn=1)
        self.grism  = ImageData(hdulist=hdu, sci_extn=2)
        
        self.contam = hdu['CONTAM'].data*1
        self.model = hdu['MODEL'].data*1
        
        if ('REF',1) in hdu:
            direct = hdu['REF', 1].data*1
        else:
            direct = hdu['SCI', 1].data*1
        
        h0 = hdu[0].header
        
        if conf is None:
            conf_file = grismconf.get_config_filename(self.direct.instrument,
                                                      self.direct.filter,
                                                      self.grism.filter)
        
            conf = grismconf.load_grism_config(conf_file)
        
        if 'GROW' in self.grism.header:
            grow = self.grism.header['GROW']
        else:
            grow = 1
            
        self.beam = GrismDisperser(id=h0['ID'], direct=direct, 
                                   segmentation=hdu['SEG'].data*1,
                                   origin=self.direct.origin,
                                   pad=h0['PAD'],
                                   grow=grow, beam=h0['BEAM'], 
                                   xcenter=h0['XCENTER'],
                                   ycenter=h0['YCENTER'],
                                   conf=conf)
        
        self.grism.parent_file = h0['GPARENT']
        self.direct.parent_file = h0['DPARENT']
    
    def write_fits(self, root='beam_', clobber=True):
        """Write attributes and data to FITS file
        
        Parameters
        ----------
        root: str
            Output filename will be 
            
               '{root}_{self.id}.{self.grism.filter}.{self.beam}.fits' 
            
            with `self.id` zero-padded with 5 digits.
        
        clobber: bool
            Clobber/overwrite existing file.
        """
        h0 = pyfits.Header()
        h0['ID'] = self.beam.id, 'Object ID'
        h0['PAD'] = self.beam.pad, 'Padding of input image'
        h0['BEAM'] = self.beam.beam, 'Grism order ("beam")'
        h0['XCENTER'] = (self.beam.xcenter, 
                         'Offset of centroid wrt thumb center')
        h0['YCENTER'] = (self.beam.ycenter, 
                         'Offset of centroid wrt thumb center')
                         
        h0['GPARENT'] = (self.grism.parent_file, 
                         'Parent grism file')
        
        h0['DPARENT'] = (self.direct.parent_file, 
                         'Parent direct file')
        
        hdu = pyfits.HDUList([pyfits.PrimaryHDU(header=h0)])
        hdu.extend(self.direct.get_HDUList(extver=1))
        hdu.append(pyfits.ImageHDU(data=np.cast[np.int32](self.beam.seg), 
                                   header=hdu[-1].header, name='SEG'))
        
        hdu.extend(self.grism.get_HDUList(extver=2))
        hdu.append(pyfits.ImageHDU(data=self.contam, header=hdu[-1].header,
                                   name='CONTAM'))
                                   
        hdu.append(pyfits.ImageHDU(data=self.model, header=hdu[-1].header,
                                   name='MODEL'))
        
        outfile = '%s_%05d.%s.%s.fits' %(root, self.beam.id,
                                         self.grism.filter.lower(),
                                         self.beam.beam)
                                         
        hdu.writeto(outfile, clobber=clobber)
        
        return outfile
        
    def compute_model(self, *args, **kwargs):
        """Link to `self.beam.compute_model`
        
        `self.beam` is a `GrismDisperser` object.
        """
        result = self.beam.compute_model(*args, **kwargs)
        return result
        
    def get_wavelength_wcs(self, wavelength=1.3e4):
        """TBD
        """
        wcs = self.grism.wcs.deepcopy()
                
        xarr = np.arange(self.beam.lam_beam.shape[0])
        
        ### Trace properties at desired wavelength
        dx = np.interp(wavelength, self.beam.lam_beam, xarr)
        dy = np.interp(wavelength, self.beam.lam_beam, self.beam.ytrace_beam)
        dl = np.interp(wavelength, self.beam.lam_beam[1:],
                                   np.diff(self.beam.lam_beam))
                                   
        ysens = np.interp(wavelength, self.beam.lam_beam,
                                      self.beam.sensitivity_beam)
                
        ### Update CRPIX
        for cr in [wcs.sip.crpix, wcs.wcs.crpix]:
                cr[0] += dx + self.beam.sh[0]/2 + self.beam.dxfull[0]
                cr[1] += dy 
        
        ### Make SIP CRPIX match CRPIX
        for i in [0,1]:
            wcs.sip.crpix[i] = wcs.wcs.crpix[i]
                    
        ### WCS header
        header = wcs.to_header(relax=True)
        for key in header:
            if key.startswith('PC'):
                header.rename_keyword(key, key.replace('PC', 'CD'))
        
        header['LONPOLE'] = 180.
        header['RADESYS'] = 'ICRS'
        header['LTV1'] = (0.0, 'offset in X to subsection start')
        header['LTV2'] = (0.0, 'offset in Y to subsection start')
        header['LTM1_1'] = (1.0, 'reciprocal of sampling rate in X')
        header['LTM2_2'] = (1.0, 'reciprocal of sampling rate in X')
        header['INVSENS'] = (ysens, 'inverse sensitivity, 10**-17 erg/s/cm2')
        header['DLDP'] = (dl, 'delta wavelength per pixel')
        
        return header, wcs
    
    def get_2d_wcs(self, data=None):
        """TBD
        """
        h = pyfits.Header()
        h['CRPIX1'] = self.beam.sh_beam[0]/2 - self.beam.xcenter
        h['CRPIX2'] = self.beam.sh_beam[0]/2 - self.beam.ycenter
        h['CRVAL1'] = self.beam.lam_beam[0]        
        h['CD1_1'] = self.beam.lam_beam[1] - self.beam.lam_beam[0]
        h['CD1_2'] = 0.
        
        h['CRVAL2'] = -1*self.beam.ytrace_beam[0]
        h['CD2_2'] = 1.
        h['CD2_1'] = -(self.beam.ytrace_beam[1] - self.beam.ytrace_beam[0])
        
        h['CTYPE1'] = 'WAVE'
        h['CTYPE2'] = 'LINEAR'
        
        if data is None:
            data = np.zeros(self.beam.sh_beam, dtype=np.float32)
        
        hdu = pyfits.ImageHDU(data=data, header=h)
        wcs = pywcs.WCS(hdu.header)
        
        wcs.pscale = np.sqrt(wcs.wcs.cd[0,0]**2 + wcs.wcs.cd[1,0]**2)*3600.
        
        return hdu, wcs
    
    def get_sky_center(self):
        """Get WCS coordinates of the center of the direct image
        
        Returns
        -------
        ra, dec: float
            Center coordinates in decimal degrees
        """
        pix_center = np.array([self.beam.sh][::-1])/2. 
        pix_center -= np.array([self.beam.xcenter, self.beam.ycenter]) 
        for i in range(2):
            self.direct.wcs.sip.crpix[i] = self.direct.wcs.wcs.crpix[i]
            
        ra, dec = self.direct.wcs.all_pix2world(pix_center, 1)[0]
        return ra, dec
        
    def init_poly_coeffs(self, poly_order=1, fit_background=True):
        """TBD
        """
        ### Already done?
        if poly_order == self.poly_order:
            return None
        
        self.poly_order = poly_order
                
        ##### Model: (a_0 x**0 + ... a_i x**i)*continuum + line
        yp, xp = np.indices(self.beam.sh_beam)
        NX = self.beam.sh_beam[1]
        self.xpf = (xp.flatten() - NX/2.)
        self.xpf /= (NX/2.)
        
        ### Polynomial continuum arrays        
        if fit_background:
            self.n_bg = 1
            self.A_poly = [self.flat_flam*0+1]
            self.A_poly.extend([self.xpf**order*self.flat_flam 
                                for order in range(poly_order+1)])
        else:
            self.n_bg = 0
            self.A_poly = [self.xpf**order*self.flat_flam 
                                for order in range(poly_order+1)]
        
        ### Array for generating polynomial "template"
        x = (np.arange(NX) - NX/2.)/ (NX/2.)
        self.y_poly = np.array([x**order for order in range(poly_order+1)])
        self.n_poly = self.y_poly.shape[0]
        self.n_simp = self.n_poly + self.n_bg
        
        self.DoF = self.fit_mask.sum()
        
    def load_templates(self, fwhm=400, line_complexes=True):
        """TBD
        """
        # templates = ['templates/EAZY_v1.0_lines/eazy_v1.0_sed1_nolines.dat',
        # 'templates/EAZY_v1.0_lines/eazy_v1.0_sed2_nolines.dat',  
        # 'templates/EAZY_v1.0_lines/eazy_v1.0_sed3_nolines.dat',     
        # 'templates/EAZY_v1.0_lines/eazy_v1.0_sed4_nolines.dat',     
        # 'templates/EAZY_v1.0_lines/eazy_v1.0_sed5_nolines.dat',     
        # 'templates/EAZY_v1.0_lines/eazy_v1.0_sed6_nolines.dat',     
        # 'templates/cvd12_t11_solar_Chabrier.extend.dat',     
        # 'templates/dobos11/bc03_pr_ch_z02_ltau07.0_age09.2_av2.5.dat']

        templates = ['templates/EAZY_v1.0_lines/eazy_v1.0_sed3_nolines.dat',  
                     'templates/cvd12_t11_solar_Chabrier.extend.dat']     
        
        temp_list = collections.OrderedDict()
        for temp in templates:
            data = np.loadtxt(os.getenv('GRIZLI') + '/' + temp, unpack=True)
            scl = np.interp(5500., data[0], data[1])
            name = os.path.basename(temp)
            temp_list[name] = utils.SpectrumTemplate(wave=data[0],
                                                             flux=data[1]/scl)
            #plt.plot(temp_list[-1].wave, temp_list[-1].flux, label=temp, alpha=0.5)
            
        line_wavelengths = {} ; line_ratios = {}
        line_wavelengths['Ha'] = [6564.61]; line_ratios['Ha'] = [1.]
        line_wavelengths['Hb'] = [4862.68]; line_ratios['Hb'] = [1.]
        line_wavelengths['Hg'] = [4341.68]; line_ratios['Hg'] = [1.]
        line_wavelengths['Hd'] = [4102.892]; line_ratios['Hd'] = [1.]
        line_wavelengths['OIIIx'] = [4364.436]; line_ratios['OIIIx'] = [1.]
        line_wavelengths['OIII'] = [5008.240, 4960.295]; line_ratios['OIII'] = [2.98, 1]
        line_wavelengths['OIII+Hb'] = [5008.240, 4960.295, 4862.68]; line_ratios['OIII+Hb'] = [2.98, 1, 3.98/8.]
        
        line_wavelengths['OIII+Hb+Ha'] = [5008.240, 4960.295, 4862.68, 6564.61]; line_ratios['OIII+Hb+Ha'] = [2.98, 1, 3.98/10., 3.98/10.*2.86]

        line_wavelengths['OIII+Hb+Ha+SII'] = [5008.240, 4960.295, 4862.68, 6564.61, 6718.29, 6732.67]
        line_ratios['OIII+Hb+Ha+SII'] = [2.98, 1, 3.98/10., 3.98/10.*2.86*4, 3.98/10.*2.86/10.*4, 3.98/10.*2.86/10.*4]

        line_wavelengths['OII'] = [3729.875]; line_ratios['OII'] = [1]
        line_wavelengths['OI'] = [6302.046]; line_ratios['OI'] = [1]

        line_wavelengths['Ha+SII'] = [6564.61, 6718.29, 6732.67]; line_ratios['Ha+SII'] = [1., 1./10, 1./10]
        line_wavelengths['SII'] = [6718.29, 6732.67]; line_ratios['SII'] = [1., 1.]
        
        if line_complexes:
            #line_list = ['Ha+SII', 'OIII+Hb+Ha', 'OII']
            line_list = ['Ha+SII', 'OIII+Hb', 'OII']
        else:
            line_list = ['Ha', 'SII', 'OIII', 'Hb', 'OII']
            #line_list = ['Ha', 'SII']
            
        for line in line_list:
            scl = line_ratios[line]/np.sum(line_ratios[line])
            for i in range(len(scl)):
                line_i = utils.SpectrumTemplate(wave=line_wavelengths[line][i], 
                                          flux=None, fwhm=fwhm, velocity=True)
                                          
                if i == 0:
                    line_temp = line_i*scl[i]
                else:
                    line_temp = line_temp + line_i*scl[i]
            
            temp_list['line %s' %(line)] = line_temp
                                     
        return temp_list
                      
    def fit_at_z(self, z=0., templates={}, fitter='lstsq', poly_order=3):
        """TBD
        """
        import copy
        
        import sklearn.linear_model
        import numpy.linalg
        
        self.init_poly_coeffs(poly_order=poly_order)
        
        NTEMP = len(self.A_poly)
        A_list = copy.copy(self.A_poly)
        ok_temp = np.ones(NTEMP+len(templates), dtype=bool)
        
        for i, key in enumerate(templates.keys()):
            NTEMP += 1
            temp = templates[key].zscale(z, 1.)
            spectrum_1d = [temp.wave, temp.flux]
            
            if ((temp.wave[0] > self.beam.lam_beam[-1]) | 
                (temp.wave[-1] < self.beam.lam_beam[0])):
                
                A_list.append(self.flat_flam*1)
                ok_temp[NTEMP-1] = False
                #print 'skip TEMP: %d, %s' %(i, key)
                continue
            else:
                pass
                #print 'TEMP: %d' %(i)
                
            temp_model = self.compute_model(spectrum_1d=spectrum_1d, 
                                            in_place=False)
            
            ### Test that model spectrum has non-zero pixel values
            #print 'TEMP: %d, %.3f' %(i, temp_model[self.fit_mask].max()/temp_model.max())
            if temp_model[self.fit_mask].max()/temp_model.max() < 0.2:
                #print 'skipx TEMP: %d, %s' %(i, key)
                ok_temp[NTEMP-1] = False
                                            
            A_list.append(temp_model)
        
        A = np.vstack(A_list).T
        out_coeffs = np.zeros(NTEMP)
        
        ### LSTSQ coefficients
        if fitter == 'lstsq':
            out = numpy.linalg.lstsq(A[self.fit_mask, :][:, ok_temp],
                                     self.scif[self.fit_mask])
            lstsq_coeff, residuals, rank, s = out
            coeffs = lstsq_coeff
        else:
            clf = sklearn.linear_model.LinearRegression()
            status = clf.fit(A[self.fit_mask, :][:, ok_temp],
                             self.scif[self.fit_mask])
            coeffs = clf.coef_
        
        out_coeffs[ok_temp] = coeffs
        model = np.dot(A, out_coeffs)
        model_2d = model.reshape(self.beam.sh_beam)

        chi2 = np.sum(((self.scif - model)**2*self.ivarf)[self.fit_mask])
        
        return A, out_coeffs, chi2, model_2d
    
    def fit_redshift(self, prior=None, poly_order=1, fwhm=500,
                     make_figure=True, zr=None, dz=None, verbose=True):
        """TBD
        """
        # if False:
        #     reload(grizlidev.utils); utils = grizlidev.utils
        #     reload(grizlidev.utils_c); reload(grizlidev.model); 
        #     reload(grizlidev.grismconf); reload(grizlidev.utils); reload(grizlidev.multifit); reload(grizlidev); reload(grizli)
        # 
        #     beams = []
        #     if id in flt.object_dispersers:
        #         b = flt.object_dispersers[id]['A']
        #         beam = grizli.model.BeamCutout(flt, b, conf=flt.conf)
        #         #print beam.grism.pad, beam.beam.grow
        #         beams.append(beam)
        #     else:
        #         print flt.grism.parent_file, 'ID %d not found' %(id)
        # 
        #     #plt.imshow(beam.beam.direct*(beam.beam.seg == id), interpolation='Nearest', origin='lower', cmap='viridis_r')
        #     self = beam
        # 
        #     #poly_order = 3
        
        if self.grism.filter == 'G102':
            if zr is None:
                zr = [0.78e4/6563.-1, 1.2e4/5007.-1]
            if dz is None:
                dz = [0.001, 0.0005]
        
        if self.grism.filter == 'G141':
            if zr is None:
                zr = [1.1e4/6563.-1, 1.65e4/5007.-1]
            if dz is None:
                dz = [0.003, 0.0005]
        
        zgrid = utils.log_zgrid(zr, dz=dz[0])
        NZ = len(zgrid)
        
        templates = self.load_templates(fwhm=fwhm)
        NTEMP = len(templates)
        
        out = self.fit_at_z(z=0., templates=templates, fitter='lstsq',
                            poly_order=poly_order)
                            
        A, coeffs, chi2, model_2d = out
        
        chi2 = np.zeros(NZ)
        coeffs = np.zeros((NZ, coeffs.shape[0]))
        
        for i in xrange(NZ):
            out = self.fit_at_z(z=zgrid[i], templates=templates,
                                fitter='lstsq', poly_order=poly_order)
            
            A, coeffs[i,:], chi2[i], model_2d = out
            if verbose:
                print utils.no_newline + '%.4f %9.1f' %(zgrid[i], chi2[i])
        
        # peaks
        import peakutils
        chi2nu = (chi2.min()-chi2)/self.DoF
        indexes = peakutils.indexes((chi2nu+0.01)*(chi2nu > -0.004), thres=0.003, min_dist=20)
        num_peaks = len(indexes)
        # plt.plot(zgrid, (chi2-chi2.min())/ self.DoF)
        # plt.scatter(zgrid[indexes], (chi2-chi2.min())[indexes]/ self.DoF, color='r')
        
        
        ### zoom
        if ((chi2.max()-chi2.min())/self.DoF > 0.01) & (num_peaks < 5):
            threshold = 0.01
        else:
            threshold = 0.001
        
        zgrid_zoom = utils.zoom_zgrid(zgrid, chi2/self.DoF, threshold=threshold, factor=10)
        NZOOM = len(zgrid_zoom)
        
        chi2_zoom = np.zeros(NZOOM)
        coeffs_zoom = np.zeros((NZOOM, coeffs.shape[1]))

        for i in xrange(NZOOM):
            out = self.fit_at_z(z=zgrid_zoom[i], templates=templates,
                                fitter='lstsq', poly_order=poly_order)

            A, coeffs_zoom[i,:], chi2_zoom[i], model_2d = out
            if verbose:
                print utils.no_newline + '- %.4f %9.1f' %(zgrid_zoom[i],
                                                          chi2_zoom[i])
    
        zgrid = np.append(zgrid, zgrid_zoom)
        chi2 = np.append(chi2, chi2_zoom)
        coeffs = np.append(coeffs, coeffs_zoom, axis=0)
    
        so = np.argsort(zgrid)
        zgrid = zgrid[so]
        chi2 = chi2[so]
        coeffs=coeffs[so,:]
        
        ### Best redshift
        templates = self.load_templates(line_complexes=False, fwhm=fwhm)
        zbest = zgrid[np.argmin(chi2)]
        out = self.fit_at_z(z=zbest, templates=templates,
                            fitter='lstsq', poly_order=poly_order)
        
        A, coeffs_full, chi2_best, model_full = out
        
        ## Continuum fit
        mask = np.isfinite(coeffs_full)
        for i, key in enumerate(templates.keys()):
            if key.startswith('line'):
                mask[self.n_simp+i] = False
            
        model_continuum = np.dot(A, coeffs_full*mask)
        model_continuum = model_continuum.reshape(self.beam.sh_beam)
                
        ### 1D spectrum
        model1d = utils.SpectrumTemplate(wave=self.beam.lam, 
                        flux=np.dot(self.y_poly.T, 
                              coeffs_full[self.n_bg:self.n_poly+self.n_bg]))
        
        cont1d = model1d*1
        
        line_flux = collections.OrderedDict()
        for i, key in enumerate(templates.keys()):
            temp_i = templates[key].zscale(zbest, coeffs_full[self.n_simp+i])
            model1d += temp_i
            if not key.startswith('line'):
                cont1d += temp_i
            else:
                line_flux[key.split()[1]] = (coeffs_full[self.n_simp+i] * 
                                             self.beam.total_flux/1.e-17)
                
                        
        fit_data = collections.OrderedDict()
        fit_data['poly_order'] = poly_order
        fit_data['fwhm'] = fwhm
        fit_data['zbest'] = zbest
        fit_data['zgrid'] = zgrid
        fit_data['A'] = A
        fit_data['coeffs'] = coeffs
        fit_data['chi2'] = chi2
        fit_data['model_full'] = model_full
        fit_data['coeffs_full'] = coeffs_full
        fit_data['line_flux'] = line_flux
        #fit_data['templates_full'] = templates
        fit_data['model_cont'] = model_continuum
        fit_data['model1d'] = model1d
        fit_data['cont1d'] = cont1d
             
        fig = None   
        if make_figure:
            fig = self.show_redshift_fit(fit_data)
            #fig.savefig('fit.pdf')
            
        return fit_data, fig
        
    def show_redshift_fit(self, fit_data):
        """Make a plot based on results from `simple_line_fit`.
        
        Parameters
        ----------
        fit_outputs: tuple
            returned data from `simple_line_fit`.  I.e., 
            
            >>> fit_outputs = BeamCutout.simple_line_fit()
            >>> fig = BeamCutout.show_simple_fit_results(fit_outputs)
        
        Returns
        -------
        fig: matplotlib.figure.Figure
            Figure object that can be optionally written to a hardcopy file.
        """
        import matplotlib.gridspec
        
        #zgrid, A, coeffs, chi2, model_best, model_continuum, model1d = fit_outputs
        
        ### Full figure
        fig = plt.figure(figsize=(12,5))
        #fig = plt.Figure(figsize=(8,4))

        ## 1D plots
        gsb = matplotlib.gridspec.GridSpec(3,1)  
        
        xspec, yspec, yerr = self.beam.optimal_extract(self.grism.data['SCI'] 
                                                        - self.contam,
                                                        ivar = self.ivar)
        
        flat_model = self.flat_flam.reshape(self.beam.sh_beam)
        xspecm, yspecm, yerrm = self.beam.optimal_extract(flat_model)
        
        out = self.beam.optimal_extract(fit_data['model_full'])
        xspecl, yspecl, yerrl = out
        
        ax = fig.add_subplot(gsb[-2:,:])
        ax.errorbar(xspec/1.e4, yspec, yerr, linestyle='None', marker='o',
                    markersize=3, color='black', alpha=0.5, 
                    label='Data (id=%d)' %(self.beam.id))
        
        ax.plot(xspecm/1.e4, yspecm, color='red', linewidth=2, alpha=0.8,
                label=r'Flat $f_\lambda$ (%s)' %(self.direct.filter))
        
        zbest = fit_data['zgrid'][np.argmin(fit_data['chi2'])]
        ax.plot(xspecl/1.e4, yspecl, color='orange', linewidth=2, alpha=0.8,
                label='Template (z=%.4f)' %(zbest))

        ax.legend(fontsize=8, loc='lower center', scatterpoints=1)

        ax.set_xlabel(r'$\lambda$'); ax.set_ylabel('flux (e-/s)')
        
        if self.grism.filter == 'G102':
            xlim = [0.7, 1.25]
        
        if self.grism.filter == 'G141':
            xlim = [1., 1.8]
            
        xt = np.arange(xlim[0],xlim[1],0.1)
        ax.set_xlim(xlim[0], xlim[1])
        ax.set_xticks(xt)

        ax = fig.add_subplot(gsb[-3,:])
        ax.plot(fit_data['zgrid'], fit_data['chi2']/self.DoF)
        for d in [1,4,9]:
            ax.plot(fit_data['zgrid'],
                    fit_data['chi2']*0+(fit_data['chi2'].min()+d)/self.DoF,
                    color='%.1f' %(d/20.))
            
        #ax.set_xticklabels([])
        ax.set_ylabel(r'$\chi^2/(\nu=%d)$' %(self.DoF))
        ax.set_xlabel('z')
        ax.set_xlim(fit_data['zgrid'][0], fit_data['zgrid'][-1])
        
        # axt = ax.twiny()
        # axt.set_xlim(np.array(ax.get_xlim())*1.e4/6563.-1)
        # axt.set_xlabel(r'$z_\mathrm{H\alpha}$')

        ## 2D spectra
        gst = matplotlib.gridspec.GridSpec(4,1)  
        if 'viridis_r' in plt.colormaps():
            cmap = 'viridis_r'
        else:
            cmap = 'cubehelix_r'

        ax = fig.add_subplot(gst[0,:])
        ax.imshow(self.grism.data['SCI'], vmin=-0.05, vmax=0.2, cmap=cmap,
                  interpolation='Nearest', origin='lower', aspect='auto')
        ax.set_ylabel('Observed')

        ax = fig.add_subplot(gst[1,:])
        mask2d = self.fit_mask.reshape(self.beam.sh_beam)
        ax.imshow((self.grism.data['SCI'] - self.contam)*mask2d, 
                  vmin=-0.05, vmax=0.2, cmap=cmap,
                  interpolation='Nearest', origin='lower', aspect='auto')
        ax.set_ylabel('Masked')
        
        ax = fig.add_subplot(gst[2,:])
        ax.imshow(fit_data['model_full']+self.contam, vmin=-0.05, vmax=0.2,
                  cmap=cmap, interpolation='Nearest', origin='lower',
                  aspect='auto')
        
        ax.set_ylabel('Model')

        ax = fig.add_subplot(gst[3,:])
        ax.imshow(self.grism.data['SCI']-fit_data['model_full']-self.contam,
                  vmin=-0.05, vmax=0.2, cmap=cmap, interpolation='Nearest',
                  origin='lower', aspect='auto')
        ax.set_ylabel('Resid.')

        for ax in fig.axes[-4:]:
            self.beam.twod_axis_labels(wscale=1.e4, 
                                       limits=[xlim[0], xlim[1], 0.1],
                                       mpl_axis=ax)
            self.beam.twod_xlim(xlim, wscale=1.e4, mpl_axis=ax)
            ax.set_yticklabels([])

        ax.set_xlabel(r'$\lambda$')
        
        for ax in fig.axes[-4:-1]:
            ax.set_xticklabels([])
            
        gsb.tight_layout(fig, pad=0.1,h_pad=0.01, rect=(0,0,0.5,1))
        gst.tight_layout(fig, pad=0.1,h_pad=0.01, rect=(0.5,0.01,1,0.98))
        
        return fig
    
        
    def simple_line_fit(self, fwhm=48., grid=[1.12e4, 1.65e4, 1, 4],
                        fitter='lstsq', poly_order=3):
        """Function to fit a Gaussian emission line and a polynomial continuum
        
        Parameters
        ----------
        fwhm: float
            FWHM of the emission line
        
        grid: [l0, l1, dl, skip]
            The base wavelength array will be generated like 
            
            >>> wave = np.arange(l0, l1, dl) 
            
            and lines will be generated every `skip` wavelength grid points:
            
            >>> line_centers = wave[::skip]
        
        fitter: 'lstsq' or 'sklearn'
            Least-squares fitting function for determining template
            normalization coefficients.
        
        order: int (>= 0)
            Polynomial order to use for the continuum
        
        Returns
        -------
        line_centers: length N array
            emission line center positions
        
        coeffs: (N, M) array where M = (poly_order+1+1)
            Normalization coefficients for the continuum and emission line
            templates.
            
        chi2: array
            Chi-squared evaluated at each line_centers[i]
        
        ok_data: ndarray
            Boolean mask of pixels used for the Chi-squared calculation. 
            Consists of non-masked DQ pixels, non-zero ERR pixels and pixels
            where `self.model > 0.03*self.model.max()` for the flat-spectrum 
            model.
        
        
        best_model: ndarray
            2D array with best-fit continuum + line model
            
        best_model_cont: ndarray
            2D array with Best-fit continuum-only model.
        
        best_line_center: float
            wavelength where chi2 is minimized.
            
        best_line_flux: float
            Emission line flux where chi2 is minimized
        """        
        ### Test fit
        import sklearn.linear_model
        import numpy.linalg
        clf = sklearn.linear_model.LinearRegression()
                
        ### Continuum
        self.beam.compute_model()
        self.modelf = self.model.flatten()
        
        ### OK data where the 2D model has non-zero flux
        ok_data = (~self.mask.flatten()) & (self.ivar.flatten() != 0)
        ok_data &= (self.modelf > 0.03*self.modelf.max())
        
        ### Flat versions of sci/ivar arrays
        scif = (self.grism.data['SCI'] - self.contam).flatten()
        ivarf = self.ivar.flatten()
        
        ##### Model: (a_0 x**0 + ... a_i x**i)*continuum + line
        yp, xp = np.indices(self.beam.sh_beam)
        xpf = (xp.flatten() - self.beam.sh_beam[1]/2.)
        xpf /= (self.beam.sh_beam[1]/2)
        
        ### Polynomial continuum arrays
        A_list = [xpf**order*self.modelf for order in range(poly_order+1)]
        
        # Extra element for the computed line model
        A_list.append(self.modelf*1)
        A = np.vstack(A_list).T
        
        ### Normalized Gaussians on a grid
        waves = np.arange(grid[0], grid[1], grid[2])
        line_centers = waves[grid[3]/2::grid[3]]
        
        rms = fwhm/2.35
        gaussian_lines = np.exp(-(line_centers[:,None]-waves)**2/2/rms**2)
        gaussian_lines /= np.sqrt(2*np.pi*rms**2)
        
        N = len(line_centers)
        coeffs = np.zeros((N, A.shape[1]))
        chi2 = np.zeros(N)
        chi2min = 1e30
        
        ### Loop through line models and fit for template coefficients
        ### Compute chi-squared.
        for i in range(N):
            self.beam.compute_model(spectrum_1d=[waves, gaussian_lines[i,:]])
                                                 
            A[:,-1] = self.model.flatten()
            if fitter == 'lstsq':
                out = numpy.linalg.lstsq(A[ok_data,:], scif[ok_data])
                lstsq_coeff, residuals, rank, s = out
                coeffs[i,:] += lstsq_coeff
                model = np.dot(A, lstsq_coeff)
            else:
                status = clf.fit(A[ok_data,:], scif[ok_data])
                coeffs[i,:] = clf.coef_
                model = np.dot(A, clf.coef_)

            chi2[i] = np.sum(((scif-model)**2*ivarf)[ok_data])
            
            if chi2[i] < chi2min:
                chi2min = chi2[i]
        
        #print chi2
        ix = np.argmin(chi2)
        self.beam.compute_model(spectrum_1d=[waves, gaussian_lines[ix,:]])
        A[:,-1] = self.model.flatten()
        best_coeffs = coeffs[ix,:]*1
        best_model = np.dot(A, best_coeffs).reshape(self.beam.sh_beam)
        
        ### Continuum
        best_coeffs_cont = best_coeffs*1
        best_coeffs_cont[-1] = 0.
        best_model_cont = np.dot(A, best_coeffs_cont)
        best_model_cont = best_model_cont.reshape(self.beam.sh_beam)

        best_line_center = line_centers[ix]
        best_line_flux = coeffs[ix,-1]*self.beam.total_flux/1.e-17
        
        return (line_centers, coeffs, chi2, ok_data, 
                best_model, best_model_cont,
                best_line_center, best_line_flux)
    
    def show_simple_fit_results(self, fit_outputs):
        """Make a plot based on results from `simple_line_fit`.
        
        Parameters
        ----------
        fit_outputs: tuple
            returned data from `simple_line_fit`.  I.e., 
            
            >>> fit_outputs = BeamCutout.simple_line_fit()
            >>> fig = BeamCutout.show_simple_fit_results(fit_outputs)
        
        Returns
        -------
        fig: matplotlib.figure.Figure
            Figure object that can be optionally written to a hardcopy file.
        """
        import matplotlib.gridspec
        
        line_centers, coeffs, chi2, ok_data, best_model, best_model_cont, best_line_center, best_line_flux = fit_outputs
        
        ### Full figure
        fig = plt.figure(figsize=(10,5))
        #fig = plt.Figure(figsize=(8,4))

        ## 1D plots
        gsb = matplotlib.gridspec.GridSpec(3,1)  
        
        xspec, yspec, yerr = self.beam.optimal_extract(self.grism.data['SCI'] 
                                                        - self.contam,
                                                        ivar = self.ivar)
        
        flat_model = self.beam.compute_model(in_place=False)
        flat_model = flat_model.reshape(self.beam.sh_beam)
        xspecm, yspecm, yerrm = self.beam.optimal_extract(flat_model)
        
        xspecl, yspecl, yerrl = self.beam.optimal_extract(best_model)
        
        ax = fig.add_subplot(gsb[-2:,:])
        ax.errorbar(xspec/1.e4, yspec, yerr, linestyle='None', marker='o',
                    markersize=3, color='black', alpha=0.5, 
                    label='Data (id=%d)' %(self.beam.id))
        
        ax.plot(xspecm/1.e4, yspecm, color='red', linewidth=2, alpha=0.8,
                label=r'Flat $f_\lambda$ (%s)' %(self.direct.filter))
        
        ax.plot(xspecl/1.e4, yspecl, color='orange', linewidth=2, alpha=0.8,
                label='Cont+line (%.3f, %.2e)' %(best_line_center/1.e4, best_line_flux*1.e-17))

        ax.legend(fontsize=8, loc='lower center', scatterpoints=1)

        ax.set_xlabel(r'$\lambda$'); ax.set_ylabel('flux (e-/s)')

        ax = fig.add_subplot(gsb[-3,:])
        ax.plot(line_centers/1.e4, chi2/ok_data.sum())
        ax.set_xticklabels([])
        ax.set_ylabel(r'$\chi^2/(\nu=%d)$' %(ok_data.sum()))
        
        if self.grism.filter == 'G102':
            xlim = [0.7, 1.25]
        
        if self.grism.filter == 'G141':
            xlim = [1., 1.8]
            
        xt = np.arange(xlim[0],xlim[1],0.1)
        for ax in fig.axes:
            ax.set_xlim(xlim[0], xlim[1])
            ax.set_xticks(xt)

        axt = ax.twiny()
        axt.set_xlim(np.array(ax.get_xlim())*1.e4/6563.-1)
        axt.set_xlabel(r'$z_\mathrm{H\alpha}$')

        ## 2D spectra
        gst = matplotlib.gridspec.GridSpec(3,1)  
        if 'viridis_r' in plt.colormaps():
            cmap = 'viridis_r'
        else:
            cmap = 'cubehelix_r'

        ax = fig.add_subplot(gst[0,:])
        ax.imshow(self.grism.data['SCI'], vmin=-0.05, vmax=0.2, cmap=cmap,
                  interpolation='Nearest', origin='lower', aspect='auto')
        ax.set_ylabel('Observed')
        
        ax = fig.add_subplot(gst[1,:])
        ax.imshow(best_model+self.contam, vmin=-0.05, vmax=0.2, cmap=cmap,
                  interpolation='Nearest', origin='lower', aspect='auto')
        ax.set_ylabel('Model')

        ax = fig.add_subplot(gst[2,:])
        ax.imshow(self.grism.data['SCI']-best_model-self.contam, vmin=-0.05,
                  vmax=0.2, cmap=cmap, interpolation='Nearest',
                  origin='lower', aspect='auto')
        ax.set_ylabel('Resid.')

        for ax in fig.axes[-3:]:
            self.beam.twod_axis_labels(wscale=1.e4, 
                                       limits=[xlim[0], xlim[1], 0.1],
                                       mpl_axis=ax)
            self.beam.twod_xlim(xlim, wscale=1.e4, mpl_axis=ax)
            ax.set_yticklabels([])

        ax.set_xlabel(r'$\lambda$')
        
        for ax in fig.axes[-3:-1]:
            ax.set_xticklabels([])
            
        gsb.tight_layout(fig, pad=0.1,h_pad=0.01, rect=(0,0,0.5,1))
        gst.tight_layout(fig, pad=0.1,h_pad=0.01, rect=(0.5,0.1,1,0.9))
        
        return fig
        
class OldGrismFLT(object):
    """
    Scripts for simple modeling of individual grism FLT images
    
    tbd: 
        o helper functions for extracting 2D spectra
        o lots of book-keeping for handling SExtractor objects & catalogs
        ...
        
    """
    def __init__(self, flt_file='ico205lwq_flt.fits', sci_ext=('SCI',1),
                 direct_image=None, refimage=None, segimage=None, refext=0,
                 verbose=True, pad=100, shrink_segimage=True,
                 force_grism=None):
        
        ### Read the FLT FITS File
        self.flt_file = flt_file
        ### Simulation mode
        if (flt_file is None) & (direct_image is not None):
            self.flt_file = direct_image
        
        self.sci_ext = sci_ext
        
        #self.wcs = pywcs.WCS(self.im['SCI',1].header)
        #self.im = pyfits.open(self.flt_file)
        self.pad = pad
                    
        self.read_flt()
        self.flt_wcs = pywcs.WCS(self.im[tuple(sci_ext)].header)
        
        ### Padded image dimensions
        self.flt_wcs.naxis1 = self.im_header['NAXIS1']+2*self.pad
        self.flt_wcs.naxis2 = self.im_header['NAXIS2']+2*self.pad
        self.flt_wcs._naxis1 = self.flt_wcs.naxis1
        self.flt_wcs._naxis2 = self.flt_wcs.naxis2
        
        ### Add padding to WCS
        self.flt_wcs.wcs.crpix[0] += self.pad
        self.flt_wcs.wcs.crpix[1] += self.pad
        
        if self.flt_wcs.sip is not None:
            self.flt_wcs.sip.crpix[0] += self.pad
            self.flt_wcs.sip.crpix[1] += self.pad           
                
        self.refimage = refimage
        self.refimage_im = None
        
        self.segimage = segimage
        self.segimage_im = None
        self.seg = np.zeros(self.im_data['SCI'].shape, dtype=np.float32)
        
        if direct_image is not None:
            ### Case where FLT is a grism exposure and FLT direct 
            ### image provided
            if verbose:
                print '%s / Blot reference image: %s' %(self.flt_file,
                                                        refimage)
            
            self.refimage = direct_image
            self.refimage_im = pyfits.open(self.refimage)
            self.filter = self.get_filter(self.refimage_im[refext].header)
                                        
            self.photflam = photflam_list[self.filter]
            self.flam = self.refimage_im[self.sci_ext].data*self.photflam
            
            ### Bad DQ bits
            dq = self.unset_dq_bits(dq=self.refimage_im['DQ'].data,
                                    okbits=32+64)            
            self.dmask = dq == 0
            
        if refimage is not None:
            ### Case where FLT is a grism exposure and reference direct 
            ### image provided
            if verbose:
                print '%s / Blot reference image: %s' %(self.flt_file,
                                                        refimage)
            
            self.refimage_im = pyfits.open(self.refimage)
            self.filter = self.get_filter(self.refimage_im[refext].header)
                                        
            self.flam = self.get_blotted_reference(self.refimage_im,
                                                   segmentation=False)
            self.photflam = photflam_list[self.filter]
            self.flam *= self.photflam
            self.dmask = np.ones(self.flam.shape, dtype=bool)
        
        if segimage is not None:
            if verbose:
                print '%s / Blot segmentation image: %s' %(self.flt_file,
                                                           segimage)
            
            self.segimage_im = pyfits.open(self.segimage)
            if shrink_segimage:
                self.shrink_segimage_to_flt()
                
            self.process_segimage()
        
        self.pivot = photplam_list[self.filter]
                        
        # This needed for the C dispersing function
        self.clip = np.cast[np.double](self.flam*self.dmask)
        
        ### Read the configuration file.  
        ## xx generalize this to get the grism information from the FLT header
        #self.grism = self.im_header0['FILTER'].upper()
        self.grism = force_grism
        if self.grism is None:
            self.grism = self.get_filter(self.im_header0)
        
        self.instrume = self.im_header0['INSTRUME']
        
        self.conf_file = grismconf.get_config_filename(self.instrume,
                                                      self.filter, self.grism)
        
        self.conf = grismconf.load_grism_config(self.conf_file)
                
        #### Get dispersion PA, requires conf for tilt of 1st order
        self.get_dispersion_PA()
                
        ### full_model is a flattened version of the FLT image
        self.modelf = np.zeros(self.sh_pad[0]*self.sh_pad[1])
        self.model = self.modelf.reshape(self.sh_pad)
        self.idx = np.arange(self.modelf.size).reshape(self.sh_pad)
                    
    def read_flt(self):
        """
        Read 'SCI', 'ERR', and 'DQ' extensions of `self.flt_file`, 
        add padding if necessary.
        
        Store result in `self.im_data`.
        """
        
        self.im = pyfits.open(self.flt_file)
        self.im_header0 = self.im[0].header.copy()
        self.im_header = self.im[tuple(self.sci_ext)].header.copy()
        
        self.sh_flt = list(self.im[tuple(self.sci_ext)].data.shape)
        self.sh_pad = [x+2*self.pad for x in self.sh_flt]
        
        slx = slice(self.pad, self.pad+self.sh_flt[1])
        sly = slice(self.pad, self.pad+self.sh_flt[0])
        
        self.im_data = {}
        for ext in ['SCI', 'ERR', 'DQ']:
            iext = (ext, self.sci_ext[1])
            self.im_data[ext] = np.zeros(self.sh_pad,
                                         dtype=self.im[iext].data.dtype)
                                         
            self.im_data[ext][sly, slx] = self.im[iext].data*1
        
        self.im_data['DQ'] = self.unset_dq_bits(dq=self.im_data['DQ'],
                                                okbits=32+64+512)
        
        ### Negative pixels
        neg_pixels = self.im_data['SCI'] < -3*self.im_data['ERR']
        self.im_data['DQ'][neg_pixels] |= 1024
        self.im_data['SCI'][self.im_data['DQ'] > 0] = 0
        
        self.im_data_sci_background = False
    
    def get_filter(self, header):
        """
        Get simple filter name out of an HST image header.  
        
        ACS has two keywords for the two filter wheels, so just return the 
        non-CLEAR filter.
        """
        if header['INSTRUME'].strip() == 'ACS':
            for i in [1,2]:
                filter = header['FILTER%d' %(i)]
                if 'CLEAR' in filter:
                    continue
                else:
                    filter = acsfilt
        else:
            filter = header['FILTER'].upper()
        
        return filter
                
    def clean_for_mp(self):
        """
        zero out io.fits objects to make suitable for multiprocessing
        parallelization
        """
        self.im = None
        self.refimage_im = None
        self.segimage_im = None
    
    def re_init(self):
        """
        Open io.fits objects again
        """
        self.im = pyfits.open(self.flt_file)
        if self.refimage:
            self.refimage_im = pyfits.open(self.refimage)
        if self.segimage:
            self.segimage_im = pyfits.open(self.segimage)
    
    def save_generated_data(self, verbose=True):
        """
        Save flam, seg, and modelf arrays to an HDF5 file
        """
        #for self in g.FLTs:
        import h5py
        h5file = self.flt_file.replace('flt.fits','flt.model.hdf5')
        h5f = h5py.File(h5file, 'w')
        
        flam = h5f.create_dataset('flam', data=self.flam)
        flam.attrs['refimage'] = self.refimage
        flam.attrs['pad'] = self.pad
        flam.attrs['filter'] = self.filter
        flam.attrs['photflam'] = self.photflam
        flam.attrs['pivot'] = self.pivot
        
        seg = h5f.create_dataset('seg', data=self.seg, compression='gzip')
        seg.attrs['segimage'] = self.segimage
        seg.attrs['pad'] = self.pad
        
        model = h5f.create_dataset('modelf', data=self.modelf,
                                   compression='gzip')
        
        h5f.close()
    
        if verbose:
            print 'Save data to %s' %(h5file)
            
    def load_generated_data(self, verbose=True):
        """
        Load flam, seg, and modelf arrays from an HDF5 file
        """
        import h5py
        h5file = self.flt_file.replace('flt.fits','flt.model.hdf5')
        if not os.path.exists(h5file):
            return False
        
        if verbose:
            print 'Load data from %s' %(h5file)
        
        h5f = h5py.File(h5file, 'r')
        if flam in h5f:
            flam = h5f['flam']
            if flam.attrs['refimage'] != self.refimage:
                print ("`refimage` doesn't match!  saved=%s, new=%s"
                       %(flam.attrs['refimage'], self.refimage))
            else:
                self.flam = np.array(flam)
                for attr in ['pad', 'filter', 'photflam', 'pivot']:
                    self.__setattr__(attr, flam.attrs[attr])
                
            
        if 'seg' in h5f:
            seg = h5f['seg']
            if flam.attrs['segimage'] != self.segimage:
                print ("`segimage` doesn't match!  saved=%s, new=%s"
                       %(flam.attrs['segimage'], self.segimage))
            else:
                self.seg = np.array(seg)
                for attr in ['pad']:
                    self.__setattr__(attr, flam.attrs[attr])
        
        if 'modelf' in h5f:
            self.modelf = np.array(h5f['modelf'])
            self.model = self.modelf.reshape(self.sh_pad)
            
    def get_dispersion_PA(self):
        """
        Compute exact PA of the dispersion axis, including tilt of the 
        trace and the FLT WCS
        """
        from astropy.coordinates import Angle
        import astropy.units as u
                    
        ### extra tilt of the 1st order grism spectra
        x0 =  self.conf.conf['BEAMA']
        dy_trace, lam_trace = self.conf.get_beam_trace(x=507, y=507, dx=x0,
                                                       beam='A')
        
        extra = np.arctan2(dy_trace[1]-dy_trace[0], x0[1]-x0[0])/np.pi*180
                
        ### Distorted WCS
        crpix = self.flt_wcs.wcs.crpix
        xref = [crpix[0], crpix[0]+1]
        yref = [crpix[1], crpix[1]]
        r, d = self.all_pix2world(xref, yref)
        pa =  Angle((extra + 
                     np.arctan2(np.diff(r), np.diff(d))[0]/np.pi*180)*u.deg)
        
        self.dispersion_PA = pa.wrap_at(360*u.deg).value
        
    def unset_dq_bits(self, dq=None, okbits=32+64+512, verbose=False):
        """
        Unset bit flags from a (WFC3/IR) DQ array
        
        32, 64: these pixels usually seem OK
           512: blobs not relevant for grism exposures
        """
        bin_bits = np.binary_repr(okbits)
        n = len(bin_bits)
        for i in range(n):
            if bin_bits[-(i+1)] == '1':
                if verbose:
                    print 2**i
                
                dq -= (dq & 2**i)
        
        return dq
        
    def all_world2pix(self, ra, dec, idx=1, tolerance=1.e-4):
        """
        Handle awkward pywcs.all_world2pix for scalar arguments
        """
        if np.isscalar(ra):
            x, y = self.flt_wcs.all_world2pix([ra], [dec], idx,
                                 tolerance=tolerance, maxiter=100, quiet=True)
            return x[0], y[0]
        else:
            return self.flt_wcs.all_world2pix(ra, dec, idx,
                                 tolerance=tolerance, maxiter=100, quiet=True)
    
    def all_pix2world(self, x, y, idx=1):
        """
        Handle awkward pywcs.all_world2pix for scalar arguments
        """
        if np.isscalar(x):
            ra, dec = self.flt_wcs.all_pix2world([x], [y], idx)
            return ra[0], dec[0]
        else:
            return self.flt_wcs.all_pix2world(x, y, idx)
    
    def blot_catalog(self, catalog_table, ra='ra', dec='dec',
                     sextractor=False):
        """
        Make x_flt and y_flt columns of detector coordinates in `self.catalog` 
        using the image WCS and the sky coordinates in the `ra` and `dec`
        columns.
        """
        from astropy.table import Column
        
        if sextractor:
            ra, dec = 'X_WORLD', 'Y_WORLD'
            if ra.lower() in catalog_table.colnames:
                ra, dec = ra.lower(), dec.lower()
        
        
        tolerance=-4
        xy = None
        ## Was having problems with `wcs not converging` with some image
        ## headers, so was experimenting between astropy.wcs and stwcs.HSTWCS.  
        ## Problem was probably rather the header itself, so this can likely
        ## be removed and simplified
        for wcs, wcsname in zip([self.flt_wcs, 
                                 pywcs.WCS(self.im_header, relax=True)], 
                                 ['astropy.wcs', 'HSTWCS']):
            if xy is not None:
                break
            for i in range(4):    
                try:
                    xy = wcs.all_world2pix(catalog_table[ra],
                                           catalog_table[dec], 1,
                                           tolerance=np.log10(tolerance+i),
                                           quiet=True)
                    break
                except:
                    print ('%s / all_world2pix failed to ' %(wcsname) + 
                           'converge at tolerance = %d' %(tolerance+i))
                
        sh = self.im_data['SCI'].shape
        keep = ((xy[0] > 0) & 
                (xy[0] < sh[1]) & 
                (xy[1] > (self.pad-5)) & 
                (xy[1] < (self.pad+self.sh_flt[0]+5)))
                
        self.catalog = catalog_table[keep]
        
        for col in ['x_flt', 'y_flt']:
            if col in self.catalog.colnames:
                self.catalog.remove_column(col)
                
        self.catalog.add_column(Column(name='x_flt', data=xy[0][keep]))
        self.catalog.add_column(Column(name='y_flt', data=xy[1][keep]))
        
        if sextractor:
            self.catalog.rename_column('X_WORLD', 'ra')
            self.catalog.rename_column('Y_WORLD', 'dec')
            self.catalog.rename_column('NUMBER', 'id')
            
        return self.catalog
        
        if False:
            ### Compute full model
            self.modelf*=0
            for mag_lim, beams in zip([[10,24], [24,28]], ['ABCDEF', 'A']): 
                ok = ((self.catalog['MAG_AUTO'] > mag_lim[0]) &
                      (self.catalog['MAG_AUTO'] < mag_lim[1]))
                      
                so = np.argsort(self.catalog['MAG_AUTO'][ok])
                for i in range(ok.sum()):
                    ix = so[i]
                    print '%d id=%d mag=%.2f' %(i+1,
                                          self.catalog['NUMBER'][ok][ix],
                                          self.catalog['MAG_AUTO'][ok][ix])
                                          
                    for beam in beams:
                        self.compute_model(id=self.catalog['NUMBER'][ok][ix],
                                           x=self.catalog['x_flt'][ok][ix],
                                           y=self.catalog['y_flt'][ok][ix],
                                           beam=beam,  sh=[60, 60],
                                           verbose=True, in_place=True)
        
        outm = self.compute_model(id=self.catalog['NUMBER'][ok][ix],
                                  x=self.catalog['x_flt'][ok][ix],
                                  y=self.catalog['y_flt'][ok][ix], 
                                  beam='A', sh=[60, 60], verbose=True,
                                  in_place=False)
        
    def show_catalog_positions(self, ds9=None):
        if not ds9:
            return False
        
        n = len(self.catalog)
        for i in range(n):
            try:
                ds9.set_region('circle %f %f 2' %(self.catalog['x_flt'][i],
                                              self.catalog['y_flt'][i]))
            except:
                ds9.set('regions', 'circle %f %f 5\n' %(
                                              self.catalog['x_flt'][i],
                                              self.catalog['y_flt'][i]))
                                              
        if False:
            ok = catalog_table['MAG_AUTO'] < 23
    
    def shrink_segimage_to_flt(self):
        """
        Make a cutout of the larger reference image around the desired FLT
        image to make blotting faster for large reference images.
        """
        
        im = self.segimage_im
        ext = 0
        
        #ref_wcs = stwcs.wcsutil.HSTWCS(im, ext=ext)
        ref_wcs = pywcs.WCS(im[ext].header)
        
        naxis = self.im_header['NAXIS1'], self.im_header['NAXIS2']
        xflt = [-self.pad*2, naxis[0]+self.pad*2, 
                naxis[0]+self.pad*2, -self.pad*2]
                
        yflt = [-self.pad*2, -self.pad*2, 
                naxis[1]+self.pad*2, naxis[1]+self.pad*2]
                
        raflt, deflt = self.flt_wcs.all_pix2world(xflt, yflt, 0)
        xref, yref = np.cast[int](ref_wcs.all_world2pix(raflt, deflt, 0))
        ref_naxis = im[ext].header['NAXIS1'], im[ext].header['NAXIS2']
        
        xmi = np.maximum(0, xref.min())
        xma = np.minimum(ref_naxis[0], xref.max())
        slx = slice(xmi, xma)
        
        ymi = np.maximum(0, yref.min())
        yma = np.minimum(ref_naxis[1], yref.max())
        sly = slice(ymi, yma)
        
        if ((xref.min() < 0) | (yref.min() < 0) | 
            (xref.max() > ref_naxis[0]) | (yref.max() > ref_naxis[1])):
            
            print ('%s / Image cutout: x=%s, y=%s [Out of range]'
                    %(self.flt_file, slx, sly))
            return False
        else:
            print '%s / Image cutout: x=%s, y=%s' %(self.flt_file, 
                                                           slx, sly)
            
        slice_wcs = ref_wcs.slice((sly, slx))
        slice_header = im[ext].header.copy()
        hwcs = slice_wcs.to_header(relax=True)
        #slice_header = hwcs
        for k in hwcs.keys():
           if not k.startswith('PC'):
               slice_header[k] = hwcs[k]
                
        slice_data = im[ext].data[sly, slx]*1
        
        hdul = pyfits.ImageHDU(data=slice_data, header=slice_header)
        self.segimage_im = pyfits.HDUList(hdul)
        
    def process_segimage(self):
        """
        Blot the segmentation image
        """
        self.seg = self.get_blotted_reference(self.segimage_im,
                                              segmentation=True)
        self.seg_ids = None
        
    def get_segment_coords(self, id=1):
        """
        Get centroid of a given ID segment
        
        ToDo: use photutils to do this
        """
        yp, xp = np.indices(self.seg.shape)
        id_mask = self.seg == id
        norm = np.sum(self.flam[id_mask])
        xi = np.sum((xp*self.flam)[id_mask])/norm
        yi = np.sum((yp*self.flam)[id_mask])/norm
        return xi+1, yi+1, id_mask
    
    def photutils_detection(self, detect_thresh=2, grow_seg=5, gauss_fwhm=2.,
                            compute_beams=None, verbose=True,
                            save_detection=False, wcs=None):
        """
        Use photutils to detect objects and make segmentation map
        
        ToDo: abstract to general script with data/thresholds 
              and just feed in image arrays
        """
        import scipy.ndimage as nd
        
        from photutils import detect_threshold, detect_sources
        from photutils import source_properties, properties_table
        from astropy.stats import sigma_clipped_stats, gaussian_fwhm_to_sigma
        from astropy.convolution import Gaussian2DKernel
        
        ### Detection threshold
        if '_flt' in self.refimage:
            threshold = (detect_thresh * self.refimage_im['ERR'].data)
        else:
            threshold = detect_threshold(self.clip, snr=detect_thresh)
        
        ### Gaussian kernel
        sigma = gauss_fwhm * gaussian_fwhm_to_sigma    # FWHM = 2.
        kernel = Gaussian2DKernel(sigma, x_size=3, y_size=3)
        kernel.normalize()
        
        if verbose:
            print '%s: photutils.detect_sources (detect_thresh=%.1f, grow_seg=%d, gauss_fwhm=%.1f)' %(self.refimage, detect_thresh, grow_seg, gauss_fwhm)
            
        ### Detect sources
        segm = detect_sources(self.clip/self.photflam, threshold, 
                              npixels=5, filter_kernel=kernel)   
        grow = nd.maximum_filter(segm.array, grow_seg)
        self.seg = np.cast[np.float32](grow)
        
        ### Source properties catalog
        if verbose:
            print  '%s: photutils.source_properties' %(self.refimage)
        
        props = source_properties(self.clip/self.photflam, segm, wcs=wcs)
        self.catalog = properties_table(props)
        
        if verbose:
            print no_newline + ('%s: photutils.source_properties - %d objects'
                                 %(self.refimage, len(self.catalog)))
        
        #### Save outputs?
        if save_detection:
            seg_file = self.refimage.replace('.fits', '.detect_seg.fits')
            seg_cat = self.refimage.replace('.fits', '.detect.cat')
            if verbose:
                print '%s: save %s, %s' %(self.refimage, seg_file, seg_cat)
            
            pyfits.writeto(seg_file, data=self.seg, 
                           header=self.refimage_im[self.sci_ext].header,
                           clobber=True)
                
            if os.path.exists(seg_cat):
                os.remove(seg_cat)
            
            self.catalog.write(seg_cat, format='ascii.commented_header')
        
        #### Compute grism model for the detected segments 
        if compute_beams is not None:
            self.compute_full_model(compute_beams, mask=None, verbose=verbose)
                        
    def load_photutils_detection(self, seg_file=None, seg_cat=None, 
                                 catalog_format='ascii.commented_header'):
        """
        Load segmentation image and catalog, either from photutils 
        or SExtractor.  
        
        If SExtractor, use `catalog_format='ascii.sextractor'`.
        
        """
        if seg_file is None:
            seg_file = self.refimage.replace('.fits', '.detect_seg.fits')
        
        if not os.path.exists(seg_file):
            print 'Segmentation image %s not found' %(segfile)
            return False
        
        self.seg = np.cast[np.float32](pyfits.open(seg_file)[0].data)
        
        if seg_cat is None:
            seg_cat = self.refimage.replace('.fits', '.detect.cat')
        
        if not os.path.exists(seg_cat):
            print 'Segmentation catalog %s not found' %(seg_cat)
            return False
        
        self.catalog = Table.read(seg_cat, format=catalog_format)
                
    def compute_full_model(self, compute_beams=['A','B'], mask=None,
                           verbose=True, sh=[20,20]):
        """
        Compute full grism model for objects in the catalog masked with 
        the `mask` boolean array
        
        `sh` is the cutout size used to model each object
        
        """
        if mask is not None:
            cat_mask = self.catalog[mask]
        else:
            cat_mask = self.catalog
        
        if 'xcentroid' in self.catalog.colnames:
            xcol = 'xcentroid'
            ycol = 'ycentroid'
        else:
            xcol = 'x_flt'
            ycol = 'y_flt'
                
        for i in range(len(cat_mask)):
            line = cat_mask[i]
            for beam in compute_beams:
                if verbose:
                    print no_newline + ('%s: compute_model - id=%4d, beam=%s'
                                         %(self.refimage, line['id'], beam))
                
                self.compute_model(id=line['id'], x=line[xcol], y=line[ycol],
                                   sh=sh, beam=beam)
            
    def align_bright_objects(self, flux_limit=4.e-18, xspec=None, yspec=None,
                             ds9=None, max_shift=10,
                             cutout_dimensions=[14,14]):
        """
        Try aligning grism spectra based on the traces of bright objects
        """
        if self.seg_ids is None:
            self.seg_ids = np.cast[int](np.unique(self.seg)[1:])
            self.seg_flux = collections.OrderedDict()
            for id in self.seg_ids:
                self.seg_flux[id] = 0
            
            self.seg_mask = self.seg > 0
            for s, f in zip(self.seg[self.seg_mask],
                            self.flam[self.seg_mask]):
                #print s
                self.seg_flux[int(s)] += f
        
            self.seg_flux = np.array(self.seg_flux.values())
        
            ids = self.seg_ids[self.seg_flux > flux_limit]
            seg_dx = self.seg_ids*0.
            seg_dy = self.seg_ids*0.
        
        ccx0 = None
        for id in ids:
            xi, yi, id_mask = self.get_segment_coords(id)
            if ((xi > 1014-201) | (yi < cutout_dimensions[0]) | 
                (yi > 1014-20)  | (xi < cutout_dimensions[1])):
                continue
                
            beam = BeamCutout(x=xi, y=yi, cutout_dimensions=cutout_dimensions,
                              beam='A', conf=self.conf, GrismFLT=self) #direct_flam=self.flam, grism_flt=self.im)
            ix = self.seg_ids == id
            
            dx, dy, ccx, ccy = beam.align_spectrum(xspec=xspec, yspec=yspec)
            
            ### Try eazy template
            # sed = eazy.getEazySED(id-1, MAIN_OUTPUT_FILE='goodsn_3dhst.v4.1.dusty', OUTPUT_DIRECTORY='/Users/brammer/3DHST/Spectra/Release/v4.1/EazyRerun/OUTPUT/', CACHE_FILE='Same', scale_flambda=True, verbose=False, individual_templates=False)
            # dx, dy, ccx, ccy = beam.align_spectrum(xspec=sed[0], yspec=sed[1]/self.seg_flux[ix])
            plt.plot(ccx/ccx.max(), color='b', alpha=0.3)
            plt.plot(ccy/ccy.max(), color='g', alpha=0.3)
            
            if ccx0 is None:
                ccx0 = ccx*1.
                ccy0 = ccy*1.
                ncc = 0.
            else:
                ccx0 += ccx
                ccy0 += ccy
                ncc += 1
                
            if (np.abs(dx) > max_shift) | (np.abs(dy) > max_shift):
                print ('[bad] ID:%d (%7.1f,%7.1f), offset=%7.2f %7.2f' 
                       %(id, xi, yi, dx, dy))
                #break
                continue
            
            seg_dx[ix] = dx
            seg_dy[ix] = dy
            print ('ID:%d (%7.1f,%7.1f), offset=%7.2f %7.2f' 
                    %(id, xi, yi, dx, dy))
            
            if ds9:
                beam.init_dispersion(xoff=0, yoff=0)
                beam.compute_model(beam.thumb, xspec=xspec, yspec=yspec)
                m0 = beam.model*1    
                beam.init_dispersion(xoff=-dx, yoff=-dy)
                beam.compute_model(beam.thumb, xspec=xspec, yspec=yspec)
                m1 = beam.model*1
                ds9.view(beam.cutout_sci-m1)
        
        ok = seg_dx != 0
        xsh, x_rms = np.mean(seg_dx[ok]), np.std(seg_dx[ok])
        ysh, y_rms = np.mean(seg_dy[ok]), np.std(seg_dy[ok])
        print ('dx = %7.3f (%7.3f), dy = %7.3f (%7.3f)' 
                %(xsh, x_rms, ysh, y_rms))
                
        return xsh, ysh, x_rms, y_rms
    
    def update_wcs_with_shift(self, xsh=0.0, ysh=0.0, drizzle_ref=True):
        """
        Update WCS
        """
        import drizzlepac.updatehdr
                
        if hasattr(self, 'xsh'):
            self.xsh += xsh
            self.ysh += ysh
        else:
            self.xsh = xsh
            self.ysh = ysh
            
        h = self.im_header
        rd = self.all_pix2world([h['CRPIX1'], h['CRPIX1']+xsh], 
                                [h['CRPIX2'], h['CRPIX2']+ysh])
        h['CRVAL1'] = rd[0][1]
        h['CRVAL2'] = rd[1][1]
        self.im[tuple(self.sci_ext)].header = h
        #self.flt_wcs = stwcs.wcsutil.HSTWCS(self.im, ext=tuple(self.sci_ext))
        self.flt_wcs = pywcs.WCS(self.im[self.sci_ext].header)
        
        self.flt_wcs.naxis1 = self.im[sci_ext].header['NAXIS1']+2*self.pad
        self.flt_wcs.naxis2 = self.im[sci_ext].header['NAXIS2']+2*self.pad
        self.flt_wcs.wcs.crpix[0] += self.pad
        self.flt_wcs.wcs.crpix[1] += self.pad
        
        if self.flt_wcs.sip is not None:
            self.flt_wcs.sip.crpix[0] += self.pad
            self.flt_wcs.sip.crpix[1] += self.pad           
        
        if drizzle_ref:
            if (self.refimage) & (self.refimage_im is None):
                self.refimage_im = pyfits.open(self.refimage)
                 
            if self.refimage_im:
                self.flam = self.get_blotted_reference(self.refimage_im,
                                                       segmentation=False)
                self.flam *= photflam_list[self.filter]
                self.clip = np.cast[np.double](self.flam*self.dmask)
            
            if (self.segimage) & (self.segimage_im is None):
                self.segimage_im = pyfits.open(self.segimage)
            
            if self.segimage_im:
                self.process_segimage()
    
    def get_blotted_reference(self, refimage=None, segmentation=False, refext=0):
        """
        Use AstroDrizzle to blot reference / segmentation images to the FLT
        frame
        """
        #import stwcs
        import astropy.wcs
        from drizzlepac import astrodrizzle
        
        #ref = pyfits.open(refimage)
        if refimage[refext].data.dtype != np.float32:
            refimage[refext].data = np.cast[np.float32](refimage[refext].data)
        
        refdata = refimage[refext].data
        if 'ORIENTAT' in refimage[refext].header.keys():
            refimage[refext].header.remove('ORIENTAT')
            
        if segmentation:
            ## todo: allow getting these from cached outputs for 
            ##       cases of very large mosaics            
            seg_ones = np.cast[np.float32](refdata > 0)-1
        
        # refdata = np.ones(refdata.shape, dtype=np.float32)
        # seg_ones = refdata
        
        #ref_wcs = astropy.wcs.WCS(refimage[refext].header)
        #ref_wcs = stwcs.wcsutil.HSTWCS(refimage, ext=refext)
        ref_wcs = pywcs.WCS(refimage[refext].header)
        
        #flt_wcs = stwcs.wcsutil.HSTWCS(self.im, ext=('SCI',1))
        flt_wcs = self.flt_wcs
        
        for wcs in [ref_wcs, flt_wcs]:
            if (not hasattr(wcs.wcs, 'cd')) & hasattr(wcs.wcs, 'pc'):
                wcs.wcs.cd = wcs.wcs.pc
                
            if hasattr(wcs, 'idcscale'):
                if wcs.idcscale is None:
                    wcs.idcscale = np.sqrt(np.sum(wcs.wcs.cd[0,:]**2))*3600.
            else:
                wcs.idcscale = np.sqrt(np.sum(wcs.wcs.cd[0,:]**2))*3600.
            
            wcs.pscale = np.sqrt(wcs.wcs.cd[0,0]**2 +
                                 wcs.wcs.cd[1,0]**2)*3600.
            
            #print 'IDCSCALE: %.3f' %(wcs.idcscale)
            
        #print refimage.filename(), ref_wcs.idcscale, ref_wcs.wcs.cd, flt_wcs.idcscale, ref_wcs.orientat
            
        if segmentation:
            #print '\nSEGMENTATION\n\n',(seg_ones+1).dtype, refdata.dtype, ref_wcs, flt_wcs
            ### +1 here is a hack for some memory issues
            blotted_seg = astrodrizzle.ablot.do_blot(refdata+0, ref_wcs,
                                flt_wcs, 1, coeffs=True, interp='nearest',
                                sinscl=1.0, stepsize=1, wcsmap=None)
            
            blotted_ones = astrodrizzle.ablot.do_blot(seg_ones+1, ref_wcs,
                                flt_wcs, 1, coeffs=True, interp='nearest',
                                sinscl=1.0, stepsize=1, wcsmap=None)
            
            blotted_ones[blotted_ones == 0] = 1
            ratio = np.round(blotted_seg/blotted_ones)
            grow = nd.maximum_filter(ratio, size=3, mode='constant', cval=0)
            ratio[ratio == 0] = grow[ratio == 0]
            blotted = ratio
            
        else:
            #print '\nREFDATA\n\n', refdata.dtype, ref_wcs, flt_wcs
            blotted = astrodrizzle.ablot.do_blot(refdata, ref_wcs, flt_wcs, 1, coeffs=True, interp='poly5', sinscl=1.0, stepsize=10, wcsmap=None)
        
        return blotted
        
    def compute_model(self, id=0, x=588.28, y=40.54, sh=[10,10], 
                      xspec=None, yspec=None, beam='A', verbose=False,
                      in_place=True, outdata=None):
        """
        Compute a model spectrum, so simple!
        
        Compute a model in a box of size `sh` around pixels `x` and `y` 
        in the direct image.
        
        Only consider pixels in the segmentation image with value = `id`.
        
        If xspec / yspec = None, the default assumes flat flambda spectra
        
        If `in place`, update the model in `self.model` and `self.modelf`, 
        otherwise put the output in a clean array.  This latter might be slow
        if the overhead of computing a large image array is high.
        """
        xc, yc = int(x), int(y)
        xcenter = x - xc
        
        ### Get dispersion parameters at the reference position
        dy, lam = self.conf.get_beam_trace(x=x-self.pad, y=y-self.pad,
                                           dx=self.conf.dxlam[beam]+xcenter,
                                           beam=beam)
        
        ### Integer trace
        # 20 for handling int of small negative numbers    
        dyc = np.cast[int](dy+20)-20+1 
        
        ### Account for pixel centering of the trace
        yfrac = dy-np.floor(dy)
        
        ### Interpolate the sensitivity curve on the wavelength grid. 
        ysens = lam*0
        so = np.argsort(lam)
        ysens[so] = interp.interp_conserve_c(lam[so],
                                 self.conf.sens[beam]['WAVELENGTH'], 
                                 self.conf.sens[beam]['SENSITIVITY'])
        
        ### Needs term of delta wavelength per pixel for flux densities
        # ! here assumes linear dispersion
        ysens *= np.abs(lam[1]-lam[0])*1.e-17
        
        if xspec is not None:
            yspec_int = ysens*0.
            yspec_int[so] = interp.interp_conserve_c(lam[so], xspec, yspec)
            ysens *= yspec_int
                    
        x0 = np.array([yc, xc])
        slx = self.conf.dxlam[beam]+xc
        ok = (slx < self.sh_pad[1]) & (slx > 0)
        
        if in_place:
            #self.modelf *= 0
            outdata = self.modelf
        else:
            if outdata is None:
                outdata = self.modelf*0
        
        ### This is an array of indices for the spectral trace
        try:
            idxl = self.idx[dyc[ok]+yc,slx[ok]]
        except:
            if verbose:
                print ('Dispersed trace falls off the image: x=%.2f y=%.2f'
                        %(x, y))
            
            return False
            
        ### Loop over pixels in the direct FLT and add them into a final 2D
        ### spectrum (in the full (flattened) FLT frame)
        ## adds into the output array, initializing full array to zero 
        ## could be very slow
        status = disperse.disperse_grism_object(self.clip, self.seg, id, idxl,
                                                yfrac[ok], ysens[ok], outdata,
                                                x0, np.array(self.clip.shape),
                                                np.array(sh),
                                                np.array(self.sh_pad))
                
        if not in_place:
            return outdata
        else:
            return True
    
    def fit_background(self, degree=3, sn_limit=0.1, pfit=None, apply=True,
                       verbose=True, ds9=None):
        """
        Fit a 2D polynomial background model to the grism exposure, only
        condidering pixels where

          self.model < sn_limit * self.im_data['ERR']
          
        """
        from astropy.modeling import models, fitting
        
        yp, xp = np.indices(self.sh_pad)
        xp  = (xp - self.sh_pad[1]/2.)/(self.sh_flt[1]/2)
        yp  = (yp - self.sh_pad[0]/2.)/(self.sh_flt[0]/2)
        
        if pfit is None:
            mask = ((self.im_data['DQ'] == 0) &
                    (self.model/self.im_data['ERR'] < sn_limit) &
                    (self.im_data['SCI'] != 0) & 
                    (self.im_data['SCI'] > -4*self.im_data['ERR']) &
                    (self.im_data['SCI'] < 6*self.im_data['ERR']) & 
                    (self.im_data['ERR'] < 1000))
                      
            poly = models.Polynomial2D(degree=degree)
            fit = fitting.LinearLSQFitter()
            pfit = fit(poly, xp[mask], yp[mask], self.im_data['SCI'][mask])
            pout = pfit(xp, yp)
            
            if ds9:
                ds9.view((self.im_data['SCI']-pout)*mask)
        else:
            pout = pfit(xp, yp)
            
        if apply:
            if self.pad > 0:
                slx = slice(self.pad, -self.pad)
                sly = slice(self.pad, -self.pad)

            else:
                slx = slice(0, self.sh_flt[1])
                sly = slice(0, self.sh_flt[0])
                
            self.im_data['SCI'][sly, slx] -= pout[sly, slx]
            self.im_data_sci_background = True
        
        if verbose:
            print ('fit_background, %s: p0_0=%7.4f' 
                   %(self.flt_file, pfit.parameters[0]))
                
        self.fit_background_result = pfit #(pfit, xp, yp)
            
class OldBeamCutout(object):
    """
    Cutout 2D spectrum from the full frame
    """
    def __init__(self, x=588.28, y=40.54, id=0, conf=None,
                 cutout_dimensions=[10,10], beam='A', GrismFLT=None):   
                
        self.beam = beam
        self.x, self.y = x, y
        self.xc, self.yc = int(x), int(y)
        self.id = id
        
        self.xcenter = self.x-self.xc
        
        if GrismFLT is not None:
            self.pad = GrismFLT.pad
        else:
            self.pad = 0
            
        self.dx = conf.dxlam[beam]
        
        self.cutout_dimensions = cutout_dimensions
        self.shd = np.array((2*cutout_dimensions[0], 2*cutout_dimensions[1]))
        self.lld = [self.yc-cutout_dimensions[0],
                    self.xc-cutout_dimensions[1]]
        self.shg = np.array((2*cutout_dimensions[0], 
                             2*cutout_dimensions[1] + conf.nx[beam]))
        self.llg = [self.yc-cutout_dimensions[0],
                    self.xc-cutout_dimensions[1]+self.dx[0]]
        
        self.x_index = np.arange(self.shg[1])
        self.y_index = np.arange(self.shg[0])

        self.modelf = np.zeros(self.shg, dtype=np.double).flatten()
        self.model = self.modelf.reshape(self.shg)

        self.conf = conf
        self.beam = beam
        self.init_dispersion()
        
        self.thumb = None
        self.cutout_sci = None    
        self.shc = None
        self.cutout_seg = np.zeros(self.shg, dtype=np.float32)
        
        self.wave = ((np.arange(self.shg[1]) + 1 - self.cutout_dimensions[1])
                      *(self.lam[1]-self.lam[0]) + self.lam[0])
        self.contam = 0
        
        if GrismFLT is not None:
            self.thumb = self.get_flam_thumb(GrismFLT.flam)*1
            self.cutout_sci = self.get_cutout(GrismFLT.im_data['SCI'])*1
            self.cutout_dq = self.get_cutout(GrismFLT.im_data['DQ'])*1
            self.cutout_err = self.get_cutout(GrismFLT.im_data['ERR'])*1
            self.shc = self.cutout_sci.shape
            
            self.cutout_seg = self.get_flam_thumb(GrismFLT.seg,
                                                  dtype=np.float32)
            self.total_flux = np.sum(self.thumb[self.cutout_seg == self.id])
            self.clean_thumb()
            
            self.grism = GrismFLT.grism
            self.dispersion_PA = GrismFLT.dispersion_PA
            self.filter = GrismFLT.filter
            self.photflam = GrismFLT.photflam
            self.pivot = GrismFLT.pivot
            
            self.compute_ivar(mask=True)
            
        # if direct_flam is not None:
        #     self.thumb = self.get_flam_thumb(direct_flam)
        # 
        # if grism_flt is not None:
        #     self.cutout_sci = self.get_cutout(grism_flt['SCI'].data)*1
        #     self.cutout_dq = self.get_cutout(grism_flt['DQ'].data)*1
        #     self.cutout_err = self.get_cutout(grism_flt['ERR'].data)*1
        #     self.shc = self.cutout_sci.shape
            #beam.cutout[beam.cutout_dq > 0] = 0
        
        #if segm_flt is not None:
         #   self.cutout_seg = self.get_cutout(segm_flt)*1
    
    def clean_thumb(self):
        """
        zero out negative pixels in self.thumb
        """
        self.thumb[self.thumb < 0] = 0
        self.total_flux = np.sum(self.thumb[self.cutout_seg == self.id])
    
    def compute_ivar(self, mask=True):
        self.ivar = np.cast[np.float32](1/(self.cutout_err**2))
        self.ivar[(self.cutout_err == 0)] = 0.
        if mask:
            self.ivar[(self.cutout_dq > 0)] = 0
            
    def init_dispersion(self, xoff=0, yoff=0):
        """
        Allow for providing offsets to the dispersion function
        """
        
        dx = self.conf.dxlam[self.beam]+self.xcenter-xoff
        self.dy, self.lam = self.conf.get_beam_trace(x=self.x-xoff-self.pad,
                                                     y=self.y+yoff-self.pad, 
                                                     dx=dx, beam=self.beam)
        
        self.dy += yoff
        
        # 20 for handling int of small negative numbers
        self.dyc = np.cast[int](self.dy+20)+-20+1
        self.yfrac = self.dy-np.floor(self.dy)
        
        dl = np.abs(self.lam[1]-self.lam[0])
        self.ysens = interp.interp_conserve_c(self.lam,
                                     self.conf.sens[self.beam]['WAVELENGTH'],
                                     self.conf.sens[self.beam]['SENSITIVITY'])
        self.ysens *= dl/1.e17
        
        self.idxl = np.arange(np.product(self.shg)).reshape(self.shg)[self.dyc+self.cutout_dimensions[0], self.dx-self.dx[0]+self.cutout_dimensions[1]]
        
    def get_flam_thumb(self, flam_full, xoff=0, yoff=0, dtype=np.double):
        dim = self.cutout_dimensions
        return np.cast[dtype](flam_full[self.yc+yoff-dim[0]:self.yc+yoff+dim[0], self.xc+xoff-dim[1]:self.xc+xoff+dim[1]])
    
    def twod_axis_labels(self, wscale=1.e4, limits=None, mpl_axis=None):
        """
        Set x axis *tick labels* on a 2D spectrum to wavelength units
        
        Defaults to a wavelength scale of microns with wscale=1.e4
        
        Will automatically use the whole wavelength range defined by the spectrum.  To change,
        specify `limits = [x0, x1, dx]` to interpolate self.wave between x0*wscale and x1*wscale.
        """
        xarr = np.arange(len(self.wave))
        if limits:
            xlam = np.arange(limits[0], limits[1], limits[2])
            xpix = np.interp(xlam, self.wave/wscale, xarr)
        else:
            xlam = np.unique(np.cast[int](self.wave / 1.e4*10)/10.)
            xpix = np.interp(xlam, self.wave/wscale, xarr)
        
        if mpl_axis is None:
            pass
            #return xpix, xlam
        else:
            mpl_axis.set_xticks(xpix)
            mpl_axis.set_xticklabels(xlam)
    
    def twod_xlim(self, x0, x1=None, wscale=1.e4, mpl_axis=None):
        """
        Set x axis *limits* on a 2D spectrum to wavelength units
        
        defaults to a scale of microns with wscale=1.e4
        
        """
        if isinstance(x0, list):
            x0, x1 = x0[0], x0[1]
        
        xarr = np.arange(len(self.wave))
        xpix = np.interp([x0,x1], self.wave/wscale, xarr)
        
        if mpl_axis:
            mpl_axis.set_xlim(xpix)
        else:
            return xpix
            
    def compute_model(self, flam_thumb, id=0, yspec=None, xspec=None, in_place=True):
        
        x0 = np.array([self.cutout_dimensions[0], self.cutout_dimensions[0]])
        sh_thumb = np.array((self.shd[0]/2, self.shd[1]/2))  
        if in_place:
            self.modelf *= 0
            out = self.modelf
        else:
            out = self.modelf*0
            
        ynorm=1
        if xspec is not self.lam:
            if yspec is not None:
                ynorm = interp.interp_conserve_c(self.lam, xspec, yspec)
        else:
            ynorm = yspec
            
        status = disperse.disperse_grism_object(flam_thumb, self.cutout_seg, id, self.idxl, self.yfrac, self.ysens*ynorm, out, x0, self.shd, sh_thumb, self.shg)
        
        if not in_place:
            return out
            
    def get_slices(self):
        sly = slice(self.llg[0], self.llg[0]+self.shg[0])
        slx = slice(self.llg[1], self.llg[1]+self.shg[1])
        return sly, slx
        
    def get_cutout(self, data):
        sly, slx = self.get_slices()
        return data[sly, slx]
        
    def make_wcs_header(self, data=None):
        #import stwcs
        h = pyfits.Header()
        h['CRPIX1'] = self.cutout_dimensions[1]#+0.5
        h['CRPIX2'] = self.cutout_dimensions[0]#+0.5
        h['CRVAL1'] = self.lam[0]        
        h['CD1_1'] = self.lam[1]-self.lam[0]
        h['CD1_2'] = 0.
        
        h['CRVAL2'] = -self.dy[0]
        h['CD2_2'] = 1.
        h['CD2_1'] = -(self.dy[1]-self.dy[0])
        
        h['CTYPE1'] = 'WAVE'
        h['CTYPE2'] = 'LINEAR'
        
        if data is None:
            np.zeros(self.shg)
        
        data = hdul = pyfits.HDUList([pyfits.ImageHDU(data=data, header=h)])
        #wcs = stwcs.wcsutil.HSTWCS(hdul, ext=0)
        wcs = pywcs.WCS(hdul[0].header)
        
        wcs.pscale = np.sqrt(wcs.wcs.cd[0,0]**2 + wcs.wcs.cd[1,0]**2)*3600.
        
        return hdul[0], wcs
        
    def align_spectrum(self, xspec=None, yspec=None):
        """
        Try to compute alignment of the reference image using cross correlation
        """
        from astropy.modeling import models, fitting
        
        clean_cutout = self.cutout_sci*1.
        clean_cutout[self.cutout_dq > 0] = 0
        #max = np.percentile(clean_cutout[clean_cutout != 0], clip_percentile)
        #clean_cutout[(clean_cutout > max) | (clean_cutout < -3*self.cutout_err)] = 0
        clean_cutout[(clean_cutout < -3*self.cutout_err) | ~np.isfinite(self.cutout_err)] = 0.
        
        self.compute_model(self.thumb, xspec=xspec, yspec=yspec)
        
        ### Cross correlation
        cc = nd.correlate(self.model/self.model.sum(), clean_cutout/clean_cutout.sum())
        
        sh = cc.shape
        shx = sh[1]/2.; shy = sh[0]/2.

        yp, xp = np.indices(cc.shape)
        shx = sh[1]/2; shy = sh[0]/2
        xp = (xp-shx); yp = (yp-shy)

        cc[:,:shx-shy] = 0
        cc[:,shx+shy:] = 0
        ccy = cc.sum(axis=1)
        ccx = cc.sum(axis=0)
        
        #fit = fitting.LevMarLSQFitter()
        #mod = models.Polynomial1D(degree=6) #(1, 0, 1)
        fit = fitting.LinearLSQFitter()

        ix = np.argmax(ccx)
        p2 = models.Polynomial1D(degree=2)
        px = fit(p2, xp[0, ix-1:ix+2], ccx[ix-1:ix+2]/ccx.max())
        dx = -px.parameters[1]/(2*px.parameters[2])

        iy = np.argmax(ccy)
        py = fit(p2, yp[iy-1:iy+2, 0], ccy[iy-1:iy+2]/ccy.max())
        dy = -py.parameters[1]/(2*py.parameters[2])
        
        return dx, dy, ccx, ccy
        
    def optimal_extract(self, data, bin=0):        
        import scipy.ndimage as nd
                
        if not hasattr(self, 'opt_profile'):
            m = self.compute_model(self.thumb, id=self.id, in_place=False).reshape(self.shg)
            m[m < 0] = 0
            self.opt_profile = m/m.sum(axis=0)
            
        num = self.opt_profile*data*self.ivar.reshape(self.shg)
        den = self.opt_profile**2*self.ivar.reshape(self.shg)
        opt = num.sum(axis=0)/den.sum(axis=0)
        opt_var = 1./den.sum(axis=0)
        
        wave = self.wave
        
        if bin > 0:
            kern = np.ones(bin, dtype=float)/bin
            opt = nd.convolve(opt, kern)[bin/2::bin]
            opt_var = nd.convolve(opt_var, kern**2)[bin/2::bin]
            wave = self.wave[bin/2::bin]
            
        opt_rms = np.sqrt(opt_var)
        opt_rms[opt_var == 0] = 0
        
        return wave, opt, opt_rms
    
    def simple_line_fit(self, fwhm=48., grid=[1.12e4, 1.65e4, 1, 4]):
        """
        Demo: fit continuum and an emission line over a wavelength grid
        """
        import sklearn.linear_model
        clf = sklearn.linear_model.LinearRegression()
                
        ### Continuum
        self.compute_model(self.thumb, id=self.id)
        ### OK data
        ok = (self.ivar.flatten() != 0) & (self.modelf > 0.03*self.modelf.max())
        
        scif = (self.cutout_sci - self.contam).flatten()
        ivarf = self.ivar.flatten()
        
        ### Model: (ax + b)*continuum + line
        yp, xp = np.indices(self.shg)
        xpf = (xp.flatten() - self.shg[1]/2.)/(self.shg[1]/2)
        
        xpf = ((self.wave[:,None]*np.ones(self.shg[0]) - self.pivot)/1000.).T.flatten()
        A = np.vstack([xpf*self.modelf*1, self.modelf*1, self.modelf*1]).T
        
        ### Fit lines
        wave_array = np.arange(grid[0], grid[1], grid[2])
        line_centers = wave_array[grid[3]/2::grid[3]]
        
        rms = fwhm/2.35
        gaussian_lines = 1/np.sqrt(2*np.pi*rms**2)*np.exp(-(line_centers[:,None]-wave_array)**2/2/rms**2)
        
        N = len(line_centers)
        coeffs = np.zeros((N, 3))
        chi2 = np.zeros(N)
        chi2min = 1e30
        
        for i in range(N):
            self.compute_model(self.thumb, id=self.id, xspec=wave_array, yspec=gaussian_lines[i,:])
            A[:,2] = self.modelf
            status = clf.fit(A[ok,:], scif[ok])
            coeffs[i,:] = clf.coef_
            
            model = np.dot(A, clf.coef_)
            chi2[i] = np.sum(((scif-model)**2*ivarf)[ok])
            
            if chi2[i] < chi2min:
                print no_newline + '%d, wave=%.1f, chi2=%.1f, line_flux=%.1f' %(i, line_centers[i], chi2[i], coeffs[i,2]*self.total_flux/1.e-17) 
                chi2min = chi2[i]
                
        ### Best    
        ix = np.argmin(chi2)
        self.compute_model(self.thumb, id=self.id, xspec=wave_array, yspec=gaussian_lines[ix,:])
        A[:,2] = self.modelf
        model = np.dot(A, coeffs[ix,:])
        
        return line_centers, coeffs, chi2, ok, model.reshape(self.shg), line_centers[ix], coeffs[ix,2]*self.total_flux/1.e-17

        