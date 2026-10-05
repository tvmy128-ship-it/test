"""The mock Tripo's models must pass the mesh gate at the sizes real accessories have (a charm is about 1.4 stud, a pet 1.2): CHK-M04 allows 5% of
the surface to be thinner than 0.05 stud. The old plush pet's tiny feet made 6% of it thin at 1.4 stud, so a charm-sized plush failed on every Tripo
try (seeds 11, 29, 47 and the p1 route) and the demo duo waited for a manual model (found by clicking the app through, tests/e2e_ui)."""
from __future__ import annotations

import numpy as np
import pytest

from duoskin.mesh import geometry as geo
from duoskin.providers.mock import meshes as M


@pytest.mark.parametrize("kind", ["icosphere", "rounded_box", "one_sided_prop", "plush_pet"])
@pytest.mark.parametrize("height", [0.8, 1.0, 1.2, 1.4, 2.0])
def test_a_mock_model_is_thick_enough_at_accessory_sizes(kind, height):
    mesh = M.build_mesh(kind)
    v = np.asarray(mesh.vertices, float)
    v = v * (height / (v[:, 1].max() - v[:, 1].min()))
    t = geo.thickness_probe(v, np.asarray(mesh.faces), samples=800, seed=5)
    thin = float((t[np.isfinite(t)] < 0.05).mean())
    assert thin <= 0.05, f"{kind} at {height} stud: {thin:.1%} of the surface is thinner than 0.05 stud"
