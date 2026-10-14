"""Validate explicit prepared-scene water lighting requests."""
import math
import unittest

from tests import REPO_ROOT
from endfield_water_transport import (WaterEdgeBlend, WaterTransport, WaterTransportLighting,
                                      evaluate_water_edge_blend, read_water_edge_blend,
                                      evaluate_water_scene_color_weight,
                                      evaluate_water_transport, read_water_transport,
                                      read_water_transport_lighting,
                                      serialize_water_transport_lighting)


class WaterTransportLightingTests(unittest.TestCase):
    def test_source_edge_fields_and_compiled_coverage_boundaries(self) -> None:
        source = {'Optics': {'DistanceEdgeBlendFactor': 2.}, 'Appearance': {'DistanceBlend': 100.}}
        settings = read_water_edge_blend(source)
        self.assertEqual(settings, WaterEdgeBlend(2.))
        self.assertEqual(source['Appearance']['DistanceBlend'], 100.)
        for depth, distance, cosine, expected in ((0., 20., 1., 0.), (.1, 0., .5, .2),
                (.1, 10., .5, .15), (.1, 20., .5, .1), (.5, 20., 1., 1.), (3., 20., .5, 1.)):
            with self.subTest(depth=depth, distance=distance, cosine=cosine):
                self.assertAlmostEqual(evaluate_water_edge_blend(settings, depth, distance, cosine), expected)
        for value in ({}, {'Optics': {}}, {'Optics': {'DistanceEdgeBlendFactor': -1.}},
                      {'Optics': {'DistanceEdgeBlendFactor': float('nan')}},
                      {'Optics': {'DistanceEdgeBlendFactor': True}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                read_water_edge_blend(value)

    def test_source_identity_and_explicit_host_controls(self) -> None:
        value = {'map_id': 'map02', 'entity_id': 90982682,
                 'ambient_radiance': [0.18, 0.22, 0.28], 'radiance_scale': 2.0,
                 'irrelevant_future_field': 'ignored'}
        result = read_water_transport_lighting(value)
        self.assertEqual((result.map_id, result.entity_id), ('map02', 90982682))
        self.assertEqual(result.ambient_radiance, (0.18, 0.22, 0.28))
        self.assertEqual(result.radiance_scale, 2.0)
        self.assertIsNone(result.phase_atmosphere_rgb)
        self.assertIsNone(result.ambient_l1)
        self.assertEqual(value['ambient_radiance'], [0.18, 0.22, 0.28])

    def test_phase_atmosphere_rgb_is_explicit_and_independent(self) -> None:
        value = {'map_id': 'map02', 'entity_id': 90982682,
                 'ambient_radiance': [0.18, 0.22, 0.28], 'radiance_scale': 1.0,
                 'phase_atmosphere_rgb': [0.04, 0.8, 0.15]}
        result = read_water_transport_lighting(value)
        self.assertEqual(result.phase_atmosphere_rgb, (0.04, 0.8, 0.15))
        self.assertEqual(value['phase_atmosphere_rgb'], [0.04, 0.8, 0.15])

    def test_lighting_serializer_roundtrips_supported_fields_only(self) -> None:
        value = {'map_id': 'map02', 'entity_id': 90982682,
                 'ambient_radiance': [.18, .22, .28], 'radiance_scale': 1.25,
                 'phase_atmosphere_rgb': [.04, .8, .15],
                 'ambient_l1': {'coefficients': [[.2, -.3, .4, .1], [-.1, .4, .2, .2],
                                                  [.3, .1, -.5, .15]],
                                'environment_global_scale': 1.7},
                 'future_field': 'ignored'}
        serialized = serialize_water_transport_lighting(read_water_transport_lighting(value))
        self.assertEqual(read_water_transport_lighting(serialized), read_water_transport_lighting(value))
        self.assertNotIn('future_field', serialized)
        self.assertEqual(value['ambient_l1']['coefficients'][0][1], -.3)

    def test_lighting_constructor_requires_explicit_optional_phase_field(self) -> None:
        result = WaterTransportLighting('map02', 90982682, (0.18, 0.22, 0.28), 1.0, None, None)
        self.assertIsNone(result.phase_atmosphere_rgb)

    def test_explicit_ambient_l1_retains_signed_channels_and_input(self) -> None:
        values = [[.2, -.3, .4, .1], [-.1, .4, .2, .2], [.3, .1, -.5, .15]]
        source = {'coefficients': values, 'environment_global_scale': 1.7, 'future_extra': 'ignored'}
        row = {'map_id': 'map02', 'entity_id': 90982682, 'ambient_radiance': [.18, .22, .28],
               'radiance_scale': 1., 'ambient_l1': source}
        result = read_water_transport_lighting(row)
        self.assertEqual(result.ambient_l1.coefficients, tuple(tuple(channel) for channel in values))
        self.assertEqual(result.ambient_l1.environment_global_scale, 1.7)
        self.assertEqual(source['coefficients'][0][1], -.3)
        for bad in (None, {}, {'coefficients': values}, {'coefficients': values[:2], 'environment_global_scale': 1.},
                    {'coefficients': [values[0], values[1], [0., 0., 0., float('nan')]], 'environment_global_scale': 1.},
                    {'coefficients': values, 'environment_global_scale': True}):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                read_water_transport_lighting({**row, 'ambient_l1': bad})

    def test_phase_rgb_replaces_only_the_direct_in_scattering_source(self) -> None:
        settings = WaterTransport(
            absorption_near=(0.10, 0.15, 0.20),
            absorption_far=(0.12, 0.17, 0.23),
            scattering=(0.20, 0.30, 0.10),
            safe_distance=5.0,
            phase_mix=0.3,
            environment_intensity=1.0,
            scene_color_environment_influence=0.0,
        )
        depth, view_y, light_y, phase_cosine = 2.5, 0.4, 0.8, 0.25
        ambient = (0.18, 0.22, 0.28)
        host_direct = (0.9, 0.25, 0.1)
        phase_rgb = (0.04, 0.8, 0.15)
        empty_direct = (0.0, 0.0, 0.0)
        ambient_transmission, ambient_only = evaluate_water_transport(
            settings, depth, view_y, light_y, phase_cosine, ambient, empty_direct)
        host_transmission, host_result = evaluate_water_transport(
            settings, depth, view_y, light_y, phase_cosine, ambient, host_direct)
        phase_transmission, phase_result = evaluate_water_transport(
            settings, depth, view_y, light_y, phase_cosine, ambient, phase_rgb)

        self.assertEqual(host_transmission, ambient_transmission)
        self.assertEqual(phase_transmission, ambient_transmission)
        ratio = min(depth, settings.safe_distance) / settings.safe_distance
        phase_base = 1.728933334350586 - 1.707551956176758 * phase_cosine
        mie = 0.2710666060447693 / max(12.566370964050293 * phase_base ** 1.5,
                                      0.0010000000474974513)
        rayleigh = 0.05968310311436653 * (1.0 + phase_cosine * phase_cosine)
        phase = mie + (rayleigh - mie) * settings.phase_mix
        for index, (near, far, scatter, host, atmosphere) in enumerate(zip(
                settings.absorption_near, settings.absorption_far, settings.scattering,
                host_direct, phase_rgb, strict=True)):
            sigma = scatter + near + (far - near) * ratio
            rate = sigma * (1.0 + abs(view_y / max(light_y, 0.05000000074505806)))
            direct_integral = -math.expm1(-rate * depth) / rate
            host_direct_delta = host_result[index] - ambient_only[index]
            phase_direct_delta = phase_result[index] - ambient_only[index]
            self.assertAlmostEqual(host_direct_delta, scatter * direct_integral * phase * host, places=12)
            self.assertAlmostEqual(phase_direct_delta, scatter * direct_integral * phase * atmosphere, places=12)

    def test_scene_color_environment_influence_scales_only_background_weight(self) -> None:
        settings = WaterTransport(
            absorption_near=(0.10, 0.15, 0.20),
            absorption_far=(0.12, 0.17, 0.23),
            scattering=(0.20, 0.30, 0.10),
            safe_distance=5.0,
            phase_mix=0.3,
            environment_intensity=1.0,
            scene_color_environment_influence=0.5,
        )
        depth, view_y, light_y, phase_cosine = 2.5, 0.4, 0.8, 0.25
        ambient = (0.18, 0.22, 0.28)
        direct = (0.9, 0.25, 0.1)
        background = (0.3, 0.6, 0.9)
        transmission, scattering = evaluate_water_transport(
            settings, depth, view_y, light_y, phase_cosine, ambient, direct)
        background_weight = evaluate_water_scene_color_weight(
            settings, depth, view_y, light_y, phase_cosine, direct)
        zero_settings = WaterTransport(
            absorption_near=settings.absorption_near,
            absorption_far=settings.absorption_far,
            scattering=settings.scattering,
            safe_distance=settings.safe_distance,
            phase_mix=settings.phase_mix,
            environment_intensity=settings.environment_intensity,
            scene_color_environment_influence=0.0,
        )
        zero_transmission, zero_scattering = evaluate_water_transport(
            zero_settings, depth, view_y, light_y, phase_cosine, ambient, direct)
        zero_weight = evaluate_water_scene_color_weight(
            zero_settings, depth, view_y, light_y, phase_cosine, direct)
        unit_settings = WaterTransport(
            absorption_near=settings.absorption_near,
            absorption_far=settings.absorption_far,
            scattering=settings.scattering,
            safe_distance=settings.safe_distance,
            phase_mix=settings.phase_mix,
            environment_intensity=settings.environment_intensity,
            scene_color_environment_influence=1.0,
        )
        unit_weight = evaluate_water_scene_color_weight(
            unit_settings, depth, view_y, light_y, phase_cosine, direct)

        self.assertEqual(transmission, zero_transmission)
        self.assertEqual(scattering, zero_scattering)
        self.assertEqual(zero_weight, (0.0, 0.0, 0.0))
        for influence_weight, unit, t, s, zt, zs, zw, bg in zip(
                background_weight, unit_weight, transmission, scattering,
                zero_transmission, zero_scattering, zero_weight, background, strict=True):
            self.assertAlmostEqual(influence_weight, 0.5 * unit, places=14)
            with_influence = (t + influence_weight) * bg + s
            without_influence = (zt + zw) * bg + zs
            self.assertAlmostEqual(with_influence - without_influence, influence_weight * bg, places=14)

    def test_source_scene_color_environment_influence_is_required_and_nonnegative(self) -> None:
        valid = {
            'Optics': {
                'WaterRayleighAbsorptionMeter2': {'X': 0.1, 'Y': 0.2, 'Z': 0.3},
                'WaterRayleighAbsorptionMeter': {'X': 0.2, 'Y': 0.3, 'Z': 0.4},
                'WaterRayleighScatteringMeter': {'X': 0.1, 'Y': 0.1, 'Z': 0.1},
                'SafeFullAbsorpDistance': 5.0,
                'RayleighMieLerpFactor': 0.3,
                'EnvLightIntensity': 1.0,
                'SceneColorEnvInfluence': 0.5,
            },
        }
        result = read_water_transport(valid)
        self.assertEqual(result.scene_color_environment_influence, 0.5)
        self.assertEqual(valid['Optics']['SceneColorEnvInfluence'], 0.5)
        with self.assertRaisesRegex(ValueError, 'SceneColorEnvInfluence'):
            read_water_transport({'Optics': {key: value for key, value in valid['Optics'].items()
                                             if key != 'SceneColorEnvInfluence'}})
        with self.assertRaisesRegex(ValueError, 'SceneColorEnvInfluence'):
            read_water_transport({'Optics': {**valid['Optics'], 'SceneColorEnvInfluence': -0.1}})

    def test_missing_or_invalid_values_are_not_inferred(self) -> None:
        valid = {'map_id': 'map02', 'entity_id': 90982682,
                 'ambient_radiance': [0.18, 0.22, 0.28], 'radiance_scale': 1.0}
        for name in valid:
            with self.subTest(missing=name), self.assertRaises(ValueError):
                read_water_transport_lighting({key: value for key, value in valid.items() if key != name})
        for name, value in (('entity_id', True), ('ambient_radiance', [1., -1., 0.]),
                            ('ambient_radiance', [1., float('nan'), 0.]),
                            ('ambient_radiance', [1., 0.]), ('radiance_scale', 0.),
                            ('radiance_scale', float('inf')),
                            ('phase_atmosphere_rgb', None),
                            ('phase_atmosphere_rgb', [1., -1., 0.]),
                            ('phase_atmosphere_rgb', [1., float('nan'), 0.]),
                            ('phase_atmosphere_rgb', [1., 0.])):
            with self.subTest(field=name, value=value), self.assertRaises(ValueError):
                read_water_transport_lighting({**valid, name: value})


if __name__ == '__main__':
    unittest.main()
