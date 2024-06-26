# Delayed parsing of type annotations
from __future__ import annotations as __annotations__

import mitsuba as mi
import drjit as dr
import gc

from typing import Union, Any, Callable, Optional, Tuple

from mitsuba.ad.integrators.common import ADIntegrator, RBIntegrator, mis_weight, _ReparamWrapper


class TransientADIntegrator(ADIntegrator):
    """
    Abstract base class of numerous differentiable transient integrators in Mitsuba

    .. pluginparameters::

     * - max_depth
       - |int|
       - Specifies the longest path depth in the generated output image (where -1
         corresponds to :math:`\\infty`). A value of 1 will only render directly
         visible light sources. 2 will lead to single-bounce (direct-only)
         illumination, and so on. (Default: 6)
     * - rr_depth
       - |int|
       - Specifies the path depth, at which the implementation will begin to use
         the *russian roulette* path termination criterion. For example, if set to
         1, then path generation many randomly cease after encountering directly
         visible surfaces. (Default: 5)
    """
    # TODO: Add documentation for other parameters
    # note that temporal bins, exposure, initial time are measured in optical path length

    def __init__(self, props=mi.Properties()):
        super().__init__(props)

        # imported: max_depth and rr_depth

        # NOTE box, gaussian, or empty string sets it to the same as the film
        self.temporal_filter = props.get('temporal_filter', '')
        self.camera_unwarp = props.get('camera_unwarp', False)
        self.gaussian_stddev = props.get('gaussian_stddev', 2.0)
        self.progressive = props.get('progressive', 0.0)

    def to_string(self):
        # TODO add other parameters
        return f'{type(self).__name__}[max_depth = {self.max_depth},' \
               f' rr_depth = { self.rr_depth }]'

    def _prepare_los(self,
                     sensor: mi.Sensor,
                     seed: int = 0,
                     spp: int = 0,
                     aovs: list = []):

        film = sensor.film()
        sampler = sensor.sampler().clone()

        if spp != 0:
            sampler.set_sample_count(spp)

        spp = sampler.sample_count()
        sampler.set_samples_per_wavefront(spp)

        film_size = film.crop_size()

        if film.sample_border():
            film_size += 2 * film.rfilter().border_size()

        wavefront_size = dr.prod(film_size) * spp

        if wavefront_size > 2**32:
            raise Exception(
                "The total number of Monte Carlo samples required by this "
                "rendering task (%i) exceeds 2^32 = 4294967296. Please use "
                "fewer samples per pixel or render using multiple passes."
                % wavefront_size)

        sampler.seed(seed, wavefront_size)
        film.prepare(aovs)

        return sampler, spp

    def _prepare_nlos(self,
                      sensor: mi.Sensor,
                      seed: int = 0,
                      spp: int = 0,
                      aovs: list = []):

        film = sensor.film()
        sampler = sensor.sampler().clone()

        film_size = film.crop_size()
        if film.sample_border():
            film_size += 2 * film.rfilter().border_size()
        film.prepare(aovs)

        if spp == 0:
            spp = sampler.sample_count()

        # It is not possible to render more than 2^32 samples
        # in a single pass (32-bit integer)
        spp_per_pass = int((2**32 - 1) / dr.prod(film_size))
        if spp_per_pass == 0:
            raise Exception(
                "The total number of Monte Carlo samples required for one sample "
                "of this rendering task (%i) exceeds 2^32 = 4294967296. Please use "
                "a smaller film size."
                % (film_size))

        # Split into max-size jobs (maybe add reminder at the end)
        needs_remainder = spp % spp_per_pass != 0
        num_passes = spp // spp_per_pass + 1 * needs_remainder

        sampler.set_sample_count(num_passes)
        sampler.set_samples_per_wavefront(num_passes)
        sampler.seed(seed, num_passes)
        seeds = mi.UInt32(sampler.next_1d() * 2**32)

        def sampler_per_pass(i):
            if needs_remainder and i == num_passes - 1:
                spp_per_pass_i = spp % spp_per_pass
            else:
                spp_per_pass_i = spp_per_pass
            sampler_clone = sensor.sampler().clone()
            sampler_clone.set_sample_count(spp_per_pass_i)
            sampler_clone.set_samples_per_wavefront(spp_per_pass_i)
            sampler_clone.seed(seeds[i], dr.prod(film_size) * spp_per_pass_i)
            return sampler_clone

        return [(sampler_per_pass(i), spp_per_pass) for i in range(num_passes)]

    def prepare(self,
                sensor: mi.Sensor,
                seed: int = 0,
                spp: int = 0,
                aovs: list = []):
        """
        Given a sensor and a desired number of samples per pixel, this function
        computes the necessary number of Monte Carlo samples and then suitably
        seeds the sampler underlying the sensor.

        Returns the created sampler and the final number of samples per pixel
        (which may differ from the requested amount depending on the type of
        ``Sampler`` being used)

        Parameter ``sensor`` (``int``, ``mi.Sensor``):
            Specify a sensor to render the scene from a different viewpoint.

        Parameter ``seed` (``int``)
            This parameter controls the initialization of the random number
            generator during the primal rendering step. It is crucial that you
            specify different seeds (e.g., an increasing sequence) if subsequent
            calls should produce statistically independent images (e.g. to
            de-correlate gradient-based optimization steps).

        Parameter ``spp`` (``int``):
            Optional parameter to override the number of samples per pixel for the
            primal rendering step. The value provided within the original scene
            specification takes precedence if ``spp=0``.
        """
        from mitransient.sensors.nloscapturemeter import NLOSCaptureMeter
        if isinstance(sensor, NLOSCaptureMeter):
            return self._prepare_nlos(sensor, seed, spp, aovs)
        else:
            return [self._prepare_los(sensor, seed, spp, aovs)]

    def prepare_transient(self, scene: mi.Scene, sensor: mi.Sensor):
        '''
        Prepare the integrator to perform a transient simulation
        '''
        import numpy as np
        from mitransient.render.transient_block import TransientBlock

        if isinstance(sensor, int):
            sensor = scene.sensors()[sensor]

        from mitransient.sensors.nloscapturemeter import NLOSCaptureMeter
        is_nlos = False
        if isinstance(sensor, NLOSCaptureMeter):
            is_nlos = True
            if self.camera_unwarp:
                raise AssertionError(
                    'camera_unwarp is not supported for NLOSCaptureMeter. '
                    'Use account_first_and_last_bounces in the NLOSCaptureMeter plugin instead.')
            if self.temporal_filter != 'box':
                self.temporal_filter = 'box'
                mi.Log(mi.LogLevel.Warn,
                       'Setting temporal_filter to box because you are using a NLOSCaptureMeter')

        film = sensor.film()
        from mitransient.films.transient_hdr_film import TransientHDRFilm
        if not isinstance(film, TransientHDRFilm):
            raise AssertionError(
                'The film of the sensor must be of type transient_hdr_film')

        # TODO fix, here we manually call set_scene and set_shape, even though it should be called by
        # https://github.com/mitsuba-renderer/mitsuba3/blob/ff9cf94323703885068779b15be36345a2eadb89/src/render/shape.cpp#L553
        # the virtual function call does not reach child class defined in mitransient
        sensor.set_scene(scene)
        for shape in scene.shapes():
            if shape.is_sensor():
                sensor.set_shape(shape)

        # Create the transient block responsible for storing the time contribution
        crop_size = film.crop_size()
        temporal_bins = film.temporal_bins
        size = np.array([crop_size.x, crop_size.y, temporal_bins])

        def load_filter(name, **kargs):
            '''
            Shorthand for loading an specific reconstruction kernel
            '''
            kargs['type'] = name
            f = mi.load_dict(kargs)
            return f

        def get_filters(sensor, iteration=0):
            if self.temporal_filter == 'box':
                time_filter = load_filter('box')
            elif self.temporal_filter == 'gaussian':
                stddev = max(self.gaussian_stddev -
                             (iteration * self.progressive), 0.5)
                time_filter = load_filter('gaussian', stddev=stddev)
            else:
                time_filter = sensor.film().rfilter()

            return [sensor.film().rfilter(), sensor.film().rfilter(), time_filter]

        filters = get_filters(sensor)
        film.prepare_transient(
            size=size,
            channel_count=5,
            channel_use_weights=not is_nlos,
            rfilter=filters)
        self._film = film

    def add_transient_f(self, pos, ray_weight):
        '''
        Return a lambda for saving transient samples
        '''
        return lambda spec, distance, wavelengths, active: \
            self._film.add_transient_data(
                spec, distance, wavelengths, active, pos, ray_weight)

    # NOTE(diego): The only change of this function w.r.t. non-transient ADIntegrator
    # is that we add the "add_transient" parameter to the call of self.sample()

    def render(self: mi.SamplingIntegrator,
               scene: mi.Scene,
               sensor: Union[int, mi.Sensor] = 0,
               seed: int = 0,
               spp: int = 0,
               develop: bool = True,
               evaluate: bool = True) -> mi.TensorXf:

        if not develop:
            raise Exception("develop=True must be specified when "
                            "invoking AD integrators")

        if isinstance(sensor, int):
            sensor = scene.sensors()[sensor]

        film = sensor.film()

        # Disable derivatives in all of the following
        with dr.suspend_grad():
            # Prepare the film and sample generator for rendering
            prepare_result = self.prepare(
                sensor=sensor,
                seed=seed,
                spp=spp,
                aovs=self.aovs()
            )

            for sampler, spp in prepare_result:
                # Generate a set of rays starting at the sensor
                ray, weight, pos, _ = self.sample_rays(scene, sensor, sampler)

                # Launch the Monte Carlo sampling process in primal mode
                L, valid, state = self.sample(
                    mode=dr.ADMode.Primal,
                    scene=scene,
                    sampler=sampler,
                    ray=ray,
                    depth=mi.UInt32(0),
                    δL=None,
                    state_in=None,
                    reparam=None,
                    active=mi.Bool(True),
                    max_distance=self._film.end_opl(),
                    add_transient=self.add_transient_f(pos, weight)
                )

                # Prepare an ImageBlock as specified by the film
                block = film.steady.create_block()

                # Only use the coalescing feature when rendering enough samples
                block.set_coalesce(block.coalesce() and spp >= 4)

                # Accumulate into the image block
                alpha = dr.select(valid, mi.Float(1), mi.Float(0))
                if mi.has_flag(film.steady.flags(), mi.FilmFlags.Special):
                    aovs = film.steady.prepare_sample(L * weight, ray.wavelengths,
                                                      block.channel_count(), alpha=alpha)
                    block.put(pos, aovs)
                    del aovs
                else:
                    block.put(pos, ray.wavelengths, L * weight, alpha)

                # Explicitly delete any remaining unused variables
                del sampler, ray, weight, pos, L, valid, alpha
                gc.collect()

                # Perform the weight division and return an image tensor
                film.steady.put_block(block)

            self.primal_image = film.steady.develop()
            transient_image = film.transient.develop()

            return self.primal_image, transient_image

    # NOTE(diego): For now, this does not change w.r.t. ADIntegrator
    def render_forward(self: mi.SamplingIntegrator,
                       scene: mi.Scene,
                       params: Any,
                       sensor: Union[int, mi.Sensor] = 0,
                       seed: int = 0,
                       spp: int = 0) -> mi.TensorXf:
        # TODO implement render_forward
        raise NotImplementedError(
            "Check https://github.com/mitsuba-renderer/mitsuba3/blob/1e513ef94db0534f54a884f2aeab7204f6f1e3ed/src/python/python/ad/integrators/common.py")

        if isinstance(sensor, int):
            sensor = scene.sensors()[sensor]

        film = sensor.film()
        aovs = self.aovs()

        # Disable derivatives in all of the following
        with dr.suspend_grad():
            # Prepare the film and sample generator for rendering
            sampler, spp = self.prepare(sensor, seed, spp, aovs)

            # When the underlying integrator supports reparameterizations,
            # perform necessary initialization steps and wrap the result using
            # the _ReparamWrapper abstraction defined above
            if hasattr(self, 'reparam'):
                reparam = _ReparamWrapper(
                    scene=scene,
                    params=params,
                    reparam=self.reparam,
                    wavefront_size=sampler.wavefront_size(),
                    seed=seed
                )
            else:
                reparam = None

            # Generate a set of rays starting at the sensor, keep track of
            # derivatives wrt. sample positions ('pos') if there are any
            ray, weight, pos, det = self.sample_rays(scene, sensor,
                                                     sampler, reparam)

            with dr.resume_grad():
                L, valid, _ = self.sample(
                    mode=dr.ADMode.Forward,
                    scene=scene,
                    sampler=sampler,
                    ray=ray,
                    reparam=reparam,
                    active=mi.Bool(True)
                )

                block = film.steady.create_block()
                # Only use the coalescing feature when rendering enough samples
                block.set_coalesce(block.coalesce() and spp >= 4)

                # Deposit samples with gradient tracking for 'pos'.
                # After reparameterizing the camera ray, we need to evaluate
                #   Σ (fi Li det)
                #  ---------------
                #   Σ (fi det)
                if (dr.all(mi.has_flag(sensor.film().flags(), mi.FilmFlags.Special))):
                    aovs = sensor.film().prepare_sample(L * weight * det, ray.wavelengths,
                                                        block.channel_count(),
                                                        weight=det,
                                                        alpha=dr.select(valid, mi.Float(1), mi.Float(0)))
                    block.put(pos, aovs)
                    del aovs
                else:
                    block.put(
                        pos=pos,
                        wavelengths=ray.wavelengths,
                        value=L * weight * det,
                        weight=det,
                        alpha=dr.select(valid, mi.Float(1), mi.Float(0))
                    )

                # Perform the weight division and return an image tensor
                film.steady.put_block(block)
                result_img = film.steady.develop()

                dr.forward_to(result_img)

        return dr.grad(result_img)

    # NOTE(diego): For now, this does not change w.r.t. ADIntegrator
    def render_backward(self: mi.SamplingIntegrator,
                        scene: mi.Scene,
                        params: Any,
                        grad_in: mi.TensorXf,
                        sensor: Union[int, mi.Sensor] = 0,
                        seed: int = 0,
                        spp: int = 0) -> None:
        # TODO implement render_backward
        raise NotImplementedError(
            "Check https://github.com/mitsuba-renderer/mitsuba3/blob/1e513ef94db0534f54a884f2aeab7204f6f1e3ed/src/python/python/ad/integrators/common.py")

        if isinstance(sensor, int):
            sensor = scene.sensors()[sensor]

        film = sensor.film()
        aovs = self.aovs()

        # Disable derivatives in all of the following
        with dr.suspend_grad():
            # Prepare the film and sample generator for rendering
            sampler, spp = self.prepare(sensor, seed, spp, aovs)

            # When the underlying integrator supports reparameterizations,
            # perform necessary initialization steps and wrap the result using
            # the _ReparamWrapper abstraction defined above
            if hasattr(self, 'reparam'):
                reparam = _ReparamWrapper(
                    scene=scene,
                    params=params,
                    reparam=self.reparam,
                    wavefront_size=sampler.wavefront_size(),
                    seed=seed
                )
            else:
                reparam = None

            # Generate a set of rays starting at the sensor, keep track of
            # derivatives wrt. sample positions ('pos') if there are any
            ray, weight, pos, det = self.sample_rays(scene, sensor,
                                                     sampler, reparam)

            with dr.resume_grad():
                L, valid, _ = self.sample(
                    mode=dr.ADMode.Backward,
                    scene=scene,
                    sampler=sampler,
                    ray=ray,
                    reparam=reparam,
                    active=mi.Bool(True)
                )

                # Prepare an ImageBlock as specified by the film
                block = film.steady.create_block()

                # Only use the coalescing feature when rendering enough samples
                block.set_coalesce(block.coalesce() and spp >= 4)

                # Accumulate into the image block
                if mi.has_flag(sensor.film().flags(), mi.FilmFlags.Special):
                    aovs = sensor.film().prepare_sample(L * weight * det, ray.wavelengths,
                                                        block.channel_count(),
                                                        weight=det,
                                                        alpha=dr.select(valid, mi.Float(1), mi.Float(0)))
                    block.put(pos, aovs)
                    del aovs
                else:
                    block.put(
                        pos=pos,
                        wavelengths=ray.wavelengths,
                        value=L * weight * det,
                        weight=det,
                        alpha=dr.select(valid, mi.Float(1), mi.Float(0))
                    )

                sensor.film().put_block(block)

                del valid
                gc.collect()

                # This step launches a kernel
                dr.schedule(block.tensor())
                image = sensor.film().develop()

                # Differentiate sample splatting and weight division steps to
                # retrieve the adjoint radiance
                dr.set_grad(image, grad_in)
                dr.enqueue(dr.ADMode.Backward, image)
                dr.traverse(mi.Float, dr.ADMode.Backward)

            # We don't need any of the outputs here
            del ray, weight, pos, block, sampler
            gc.collect()

            # Run kernel representing side effects of the above
            dr.eval()

    # NOTE(diego): For now, this does not change w.r.t. ADIntegrator
    def sample_rays(
        self,
        scene: mi.Scene,
        sensor: mi.Sensor,
        sampler: mi.Sampler,
        reparam: Callable[[mi.Ray3f, mi.UInt32, mi.Bool],
                          Tuple[mi.Vector3f, mi.Float]] = None
    ) -> Tuple[mi.RayDifferential3f, mi.Spectrum, mi.Vector2f, mi.Float]:
        """
        Sample a 2D grid of primary rays for a given sensor

        Returns a tuple containing

        - the set of sampled rays
        - a ray weight (usually 1 if the sensor's response function is sampled
          perfectly)
        - the continuous 2D image-space positions associated with each ray

        When a reparameterization function is provided via the 'reparam'
        argument, it will be applied to the returned image-space position (i.e.
        the sample positions will be moving). The other two return values
        remain detached.
        """

        film = sensor.film()
        film_size = film.crop_size()
        rfilter = film.rfilter()
        border_size = rfilter.border_size()

        if film.sample_border():
            film_size += 2 * border_size

        spp = sampler.sample_count()

        # Compute discrete sample position
        idx = dr.arange(mi.UInt32, dr.prod(film_size) * spp)

        # Try to avoid a division by an unknown constant if we can help it
        log_spp = dr.log2i(spp)
        if 1 << log_spp == spp:
            idx >>= dr.opaque(mi.UInt32, log_spp)
        else:
            idx //= dr.opaque(mi.UInt32, spp)

        # Compute the position on the image plane
        pos = mi.Vector2i()
        pos.y = idx // film_size[0]
        pos.x = dr.fma(-film_size[0], pos.y, idx)

        if film.sample_border():
            pos -= border_size

        pos += mi.Vector2i(film.crop_offset())

        # Cast to floating point and add random offset
        pos_f = mi.Vector2f(pos) + sampler.next_2d()

        # Re-scale the position to [0, 1]^2
        scale = dr.rcp(mi.ScalarVector2f(film.crop_size()))
        offset = -mi.ScalarVector2f(film.crop_offset()) * scale
        pos_adjusted = dr.fma(pos_f, scale, offset)

        aperture_sample = mi.Vector2f(0.0)
        if sensor.needs_aperture_sample():
            aperture_sample = sampler.next_2d()

        time = sensor.shutter_open()
        if sensor.shutter_open_time() > 0:
            time += sampler.next_1d() * sensor.shutter_open_time()

        wavelength_sample = 0
        if mi.is_spectral:
            wavelength_sample = sampler.next_1d()

        with dr.resume_grad():
            ray, weight = sensor.sample_ray_differential(
                time=time,
                sample1=wavelength_sample,
                sample2=pos_adjusted,
                sample3=aperture_sample
            )

        reparam_det = 1.0

        if reparam is not None:
            if rfilter.is_box_filter():
                raise Exception(
                    "ADIntegrator detected the potential for image-space "
                    "motion due to differentiable shape or camera pose "
                    "parameters. This is, however, incompatible with the box "
                    "reconstruction filter that is currently used. Please "
                    "specify a smooth reconstruction filter in your scene "
                    "description (e.g. 'gaussian', which is actually the "
                    "default)")

            # This is less serious, so let's just warn once
            if not film.sample_border() and self.sample_border_warning:
                self.sample_border_warning = True

                mi.Log(mi.LogLevel.Warn,
                       "ADIntegrator detected the potential for image-space "
                       "motion due to differentiable shape or camera pose "
                       "parameters. To correctly account for shapes entering "
                       "or leaving the viewport, it is recommended that you set "
                       "the film's 'sample_border' parameter to True.")

            with dr.resume_grad():
                # Reparameterize the camera ray
                reparam_d, reparam_det = reparam(ray=dr.detach(ray),
                                                 depth=mi.UInt32(0))

                # TODO better understand why this is necessary
                # Reparameterize the camera ray to handle camera translations
                if dr.grad_enabled(ray.o):
                    reparam_d, _ = reparam(ray=ray, depth=mi.UInt32(0))

                # Create a fake interaction along the sampled ray and use it to
                # recompute the position with derivative tracking
                it = dr.zeros(mi.Interaction3f)
                it.p = ray.o + reparam_d
                ds, _ = sensor.sample_direction(it, aperture_sample)

                # Return a reparameterized image position
                pos_f = ds.uv + film.crop_offset()

        # With box filter, ignore random offset to prevent numerical instabilities
        splatting_pos = mi.Vector2f(pos) if rfilter.is_box_filter() else pos_f

        return ray, weight, splatting_pos, reparam_det

    # NOTE(diego): only change is the addition of the add_transient argument
    def sample(self,
               mode: dr.ADMode,
               scene: mi.Scene,
               sampler: mi.Sampler,
               ray: mi.Ray3f,
               depth: mi.UInt32,
               δL: Optional[mi.Spectrum],
               state_in: Any,
               reparam: Optional[
                   Callable[[mi.Ray3f, mi.UInt32, mi.Bool],
                            Tuple[mi.Vector3f, mi.Float]]],
               active: mi.Bool,
               add_transient) -> Tuple[mi.Spectrum, mi.Bool]:
        """
        This function does the main work of differentiable rendering and
        remains unimplemented here. It is provided by subclasses of the
        ``RBIntegrator`` interface.

        In those concrete implementations, the function performs a Monte Carlo
        random walk, implementing a number of different behaviors depending on
        the ``mode`` argument. For example in primal mode (``mode ==
        drjit.ADMode.Primal``), it behaves like a normal rendering algorithm
        and estimates the radiance incident along ``ray``.

        In forward mode (``mode == drjit.ADMode.Forward``), it estimates the
        derivative of the incident radiance for a set of scene parameters being
        differentiated. (This requires that these parameters are attached to
        the AD graph and have gradients specified via ``dr.set_grad()``)

        In backward mode (``mode == drjit.ADMode.Backward``), it takes adjoint
        radiance ``δL`` and accumulates it into differentiable scene parameters.

        You are normally *not* expected to directly call this function. Instead,
        use ``mi.render()`` , which performs various necessary
        setup steps to correctly use the functionality provided here.

        The parameters of this function are as follows:

        Parameter ``mode`` (``drjit.ADMode``)
            Specifies whether the rendering algorithm should run in primal or
            forward/backward derivative propagation mode

        Parameter ``scene`` (``mi.Scene``):
            Reference to the scene being rendered in a differentiable manner.

        Parameter ``sampler`` (``mi.Sampler``):
            A pre-seeded sample generator

        Parameter ``depth`` (``mi.UInt32``):
            Path depth of `ray` (typically set to zero). This is mainly useful
            for forward/backward differentiable rendering phases that need to
            obtain an incident radiance estimate. In this case, they may
            recursively invoke ``sample(mode=dr.ADMode.Primal)`` with a nonzero
            depth.

        Parameter ``δL`` (``mi.Spectrum``):
            When back-propagating gradients (``mode == drjit.ADMode.Backward``)
            the ``δL`` parameter should specify the adjoint radiance associated
            with each ray. Otherwise, it must be set to ``None``.

        Parameter ``state_in`` (``Any``):
            The primal phase of ``sample()`` returns a state vector as part of
            its return value. The forward/backward differential phases expect
            that this state vector is provided to them via this argument. When
            invoked in primal mode, it should be set to ``None``.

        Parameter ``reparam`` (see above):
            If provided, this callable takes a ray and a mask of active SIMD
            lanes and returns a reparameterized ray and Jacobian determinant.
            The implementation of the ``sample`` function should then use it to
            correctly account for visibility-induced discontinuities during
            differentiation.

        Parameter ``active`` (``mi.Bool``):
            This mask array can optionally be used to indicate that some of
            the rays are disabled.

        TODO(diego): Parameter ``add_transient_f`` (and document type above)
        or probably refer to non-transient RB

        The function returns a tuple ``(spec, valid, state_out)`` where

        Output ``spec`` (``mi.Spectrum``):
            Specifies the estimated radiance and differential radiance in
            primal and forward mode, respectively.

        Output ``valid`` (``mi.Bool``):
            Indicates whether the rays intersected a surface, which can be used
            to compute an alpha channel.
        """

        raise Exception('RBIntegrator does not provide the sample() method. '
                        'It should be implemented by subclasses that '
                        'specialize the abstract RBIntegrator interface.')


class TransientRBIntegrator(RBIntegrator, TransientADIntegrator):
    # Prioritizes RBIntegrator functions over TransientADIntegrator
    pass
