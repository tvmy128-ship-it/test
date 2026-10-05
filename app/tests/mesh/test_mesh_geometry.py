"""Pure numpy/scipy geometry helpers."""
from __future__ import annotations

import numpy as np

from duoskin.mesh import geometry as geo
from duoskin.mesh.boolean import box_mesh


def cube(size=1.0):
    m = box_mesh([-size / 2] * 3, [size / 2] * 3)
    return m.vertices, m.faces


def test_weld_merges_duplicates_and_keeps_face_order():
    v, f = cube()
    # split every face's vertices (24 verts) like a UV-seamed mesh
    v2 = v[f].reshape(-1, 3)
    f2 = np.arange(len(v2)).reshape(-1, 3)
    w = geo.weld(v2, f2)
    assert len(w.vertices) == 8 and len(w.faces) == 12
    assert np.allclose(w.vertices[w.faces], v[f])            # same triangles in the same order
    assert w.vmap.shape == (36,)


def test_topology_stats_of_closed_open_and_nonmanifold():
    _v, f = cube()
    st = geo.topology_stats(f)
    assert st["watertight"] and st["winding_consistent"] and st["boundary_edges"] == 0
    assert geo.topology_stats(f[:-1])["boundary_edges"] == 3 and not geo.topology_stats(f[:-1])["watertight"]
    bad = np.vstack([f, f[:1] + 0])                           # a duplicated face: 3 faces on its edges
    assert geo.topology_stats(bad)["nonmanifold_edges"] == 3
    flipped = f.copy()
    flipped[0] = flipped[0][::-1]
    assert not geo.topology_stats(flipped)["winding_consistent"]


def test_fix_winding_and_orient_outward():
    v, f = cube()
    broken = f.copy()
    broken[[0, 5, 9]] = broken[[0, 5, 9]][:, ::-1]
    fixed, flipped = geo.fix_winding(broken)
    assert geo.topology_stats(fixed)["winding_consistent"] and flipped.sum() in (3, 9)
    out, _ = geo.orient_outward(v, fixed)
    assert geo.signed_volume(v, out) > 0
    inside_out, _ = geo.orient_outward(v, f[:, ::-1])
    assert abs(geo.signed_volume(v, inside_out) - 1.0) < 1e-9


def test_signed_volume_area_and_normals():
    v, f = cube(2.0)
    assert abs(geo.signed_volume(v, f) - 8.0) < 1e-9
    assert abs(geo.surface_area(v, f) - 24.0) < 1e-9
    n, _a = geo.face_normals_areas(v, f)
    c = v[f].mean(axis=1)
    assert np.all(np.einsum("ij,ij->i", n, c) > 0)            # outward


def test_shells_and_info():
    v, f = cube()
    v2 = np.vstack([v, v + 5])
    f2 = np.vstack([f, f + 8])
    labels, count = geo.shell_labels(f2, len(v2))
    assert count == 2 and set(labels) == {0, 1}
    info = geo.shell_info(v2, f2, labels, count)
    assert all(s["closed"] and abs(s["volume"] - 1.0) < 1e-9 for s in info)


def test_winding_number_inside_outside_and_open():
    v, f = cube(2.0)
    w = geo.winding_numbers(np.array([[0, 0, 0], [3, 0, 0], [0.9, 0.9, 0.9], [1.1, 0, 0]]), v, f)
    assert np.allclose(w[:2], [1.0, 0.0], atol=1e-6) and w[2] > 0.99 and abs(w[3]) < 1e-6


def test_ray_nearest_and_thickness():
    v, f = cube(2.0)
    t = geo.ray_nearest(np.array([[0, 0, 5.0], [5, 5, 5.0]]), np.array([[0, 0, -1.0], [0, 0, -1.0]]), v, f)
    assert abs(t[0] - 4.0) < 1e-9 and np.isinf(t[1])
    th = geo.thickness_probe(v, f, samples=200)
    assert np.all(np.isfinite(th)) and abs(np.median(th) - 2.0) < 0.05
    sv, sf = cube()
    sv = sv * np.array([1, 1, 0.02])                           # a 0.02 stud slab
    assert geo.thickness_probe(sv, sf, samples=200).min() < 0.03


def test_sample_surface_is_deterministic_and_on_the_surface():
    v, f = cube()
    a, ia = geo.sample_surface(v, f, 100, seed=3)
    b, ib = geo.sample_surface(v, f, 100, seed=3)
    assert np.array_equal(a, b) and np.array_equal(ia, ib)
    assert np.allclose(np.abs(a).max(axis=1), 0.5)


def test_24_axis_rotations_are_proper_and_unique_with_identity_first():
    rots = geo.all_axis_rotations()
    assert len(rots) == 24 and np.allclose(rots[0], np.eye(3))
    assert all(abs(np.linalg.det(r) - 1) < 1e-12 for r in rots)
    assert len({tuple(r.flatten()) for r in rots}) == 24


def test_remove_unused_renumbers():
    v, f = cube()
    v2 = np.vstack([v, [[9, 9, 9]]])
    nv, nf = geo.remove_unused(v2, f)[:2]
    assert len(nv) == 8 and nf.max() == 7
