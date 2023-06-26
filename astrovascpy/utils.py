"""
Copyright (c) 2023-2023 Blue Brain Project/EPFL
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
    http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import logging
import sys
import time
from collections.abc import Iterable
from contextlib import contextmanager

import networkx as nx
import numpy as np
import pandas as pd
import psutil
import scipy as sp
from mpi4py import MPI
from scipy.signal import find_peaks_cwt
from scipy.sparse.csgraph import connected_components
from vascpy import PointVasculature

from astrovascpy.exceptions import BloodFlowError

# dtypes for the different node and edge ids. We are using np.int64 to avoid the infamous
# https://github.com/numpy/numpy/issues/15084 numpy problem. This type needs to be used for
# all returned node or edge ids.
IDS_DTYPE = np.int64

# Definition of Logger L
FORMATTER = logging.Formatter("%(levelname)s: [%(filename)s - %(funcName)s() ] %(message)s")
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(FORMATTER)
L = logging.getLogger(__name__)
L.addHandler(console_handler)

MPI_RANK = MPI.COMM_WORLD.Get_rank()


def find_neighbors(graph, section_id, segment_id):
    """Given an edge described by section_id and segment_id, find the neighbors edges.

    Args:
        graph (vasculatureAPI.PointVasculature): graph containing point vasculature skeleton.
        section_id (int): id of the corresponding section.
        segment_id (int): id of the corresponding segment.

    Returns:
        pandas.Series: neighbors_mask to filter edges with 1 node in common with the original edge.
    """
    edge_start_node = graph.edge_properties.start_node[section_id, segment_id]
    edge_end_node = graph.edge_properties.end_node[section_id, segment_id]
    neighbors_mask = (
        (graph.edge_properties.start_node == edge_start_node)
        | (graph.edge_properties.end_node == edge_end_node)
        | (graph.edge_properties.start_node == edge_end_node)
        | (graph.edge_properties.end_node == edge_start_node)
    )
    neighbors_mask[section_id, segment_id] = False
    return neighbors_mask


def find_degrees_of_neighbors(graph, node_id):
    """Given an edge described by node_id, find the neighbors edges.

    Args:
        graph (vasculatureAPI.PointVasculature): graph containing point vasculature skeleton.
        node_id (int): id of the selected node

    Returns:
        pandas.Series: neighbors_mask to filter edges with 1 node in common with the original edge
        set: nodes ids of the connected nodes to the node id.
        numpy.array: list of nodes close to the original node id
    """
    edge_start_node = graph.edge_properties.start_node == node_id
    edge_end_node = graph.edge_properties.end_node == node_id
    neighbors_mask = edge_start_node | edge_end_node
    connected_nodes = set(graph.edge_properties.start_node[neighbors_mask].to_list())
    connected_nodes |= set(graph.edge_properties.end_node[neighbors_mask].to_list())
    return neighbors_mask, connected_nodes, graph.degrees[np.array(list(connected_nodes))]


def get_main_connected_component(graph):
    """Return a graph with only the largest Connected Component (CC).

    Args:
        graph (vasculatureAPI.PointVasculature): graph containing point vasculature skeleton.

    Returns:
        vasculatureAPI.PointVasculature: largest CC point graph.
    """
    _, labels = connected_components(
        graph.adjacency_matrix.as_sparse(), directed=False, return_labels=True
    )
    largest_cc_label = np.argmax(np.unique(labels, return_counts=True)[1])
    to_keep_labels = labels == largest_cc_label
    graph_point = graph.node_properties.loc[to_keep_labels]
    index = graph_point.index
    graph_edge = graph.edge_properties[
        (graph.edge_properties["start_node"].isin(index))
        | (graph.edge_properties["end_node"].isin(index))
    ]
    return PointVasculature(graph_point, graph_edge)


def reduce_to_largest_cc(graph):  # pragma: no cover
    """Return a new graph with only the largest CC.

    Args:
        graph (vasculatureAPI.PointVasculature): graph containing point vasculature skeleton.

    Returns:
        vasculatureAPI.PointVasculature: reduced to the largest cc point graph.
    """
    _, labels = connected_components(graph.adjacency_matrix.as_sparse(), return_labels=True)
    largest_cc_label = np.argmax(np.unique(labels, return_counts=True)[1])
    to_keep_labels = labels == largest_cc_label
    return reduce_graph(graph, to_keep_labels)


def get_local_neighboors(graph, iterations, starting_nodes_id):
    """Get local neighbours.

    Args:
        graph (vasculatureAPI.PointVasculature): graph containing point vasculature skeleton.
        iterations (int): number of iterations.
        starting_nodes_id (int): start node id.

    Returns:
        numpy.array: get local neighbourg of selected nodes ids.
    """
    A = graph.adjacency_matrix.as_sparse()
    A = A + A.T

    p = np.zeros(graph.n_nodes)
    p[starting_nodes_id] = 1.0
    for _ in range(iterations):
        p += A.dot(p)
        p[p > 1] = 1
    return p > 0


def get_subset(graph, iterations=100, starting_nodes_id=None):
    """Get subset of graph as connected component from a starting node, using random walk.

    Args:
        graph (vasculatureAPI.PointVasculature): graph containing point vasculature skeleton.
        iterations (int): number of iterations.
        starting_nodes_id (int): start node id.

    Returns:
        vasculatureAPI.PointVasculature: reduced to the largest cc point graph.
    """
    if starting_nodes_id is None:
        starting_nodes_id = graph.node_properties.loc[graph.degrees == 1, "diameter"].idxmax()
    mask = get_local_neighboors(graph, iterations, starting_nodes_id)
    return reduce_graph(graph, mask)


def reduce_graph(graph, to_keep_labels):
    """Return a subgraph with to_keep_labels."""
    new_node_properties = graph.node_properties.copy()
    new_node_properties = new_node_properties[to_keep_labels]
    new_edge_properties = graph.edge_properties.loc[
        graph.edge_properties.start_node.isin(new_node_properties.index.tolist())
    ]
    new_edge_properties = new_edge_properties.loc[
        new_edge_properties.end_node.isin(new_node_properties.index.tolist())
    ]
    new_node_properties = new_node_properties.reset_index()
    new_node_properties["new_index"] = new_node_properties.index.to_list()
    new_node_properties = new_node_properties.set_index("index")
    new_edge_properties["start_node"] = new_node_properties.loc[
        new_edge_properties["start_node"], "new_index"
    ].to_list()
    new_edge_properties["end_node"] = new_node_properties.loc[
        new_edge_properties["end_node"], "new_index"
    ].to_list()
    new_node_properties = new_node_properties.set_index("new_index")
    new_edge_properties = new_edge_properties.reset_index().drop(columns=["index"])
    new_node_properties.index.name = "index"
    new_edge_properties.index.name = "index"
    return PointVasculature(new_node_properties, new_edge_properties)


def compute_edge_data(graph):
    """Compute the length, radius and volume of each edge.

    Args:
        graph (vasculatureAPI.PointVasculature): graph containing point vasculature skeleton.

    Returns:
        numpy.array: (nb_edges, ) radii of each edge (units: µm).
        numpy.array: (nb_edges, ) lengths of each edge (units: µm).
        numpy.array: (nb_edges, ) volume of each edge (units: µm^3).
    """
    positions = graph.points
    beg_nodes, end_nodes = graph.edges.T
    edge_lengths = np.linalg.norm(positions[end_nodes] - positions[beg_nodes], axis=1)
    node_radii = 0.5 * graph.diameters
    edge_radii = 0.5 * (node_radii[beg_nodes] + node_radii[end_nodes])
    edge_volume = edge_lengths * np.power(edge_radii, 2) * np.pi
    return edge_lengths, edge_radii, edge_volume


def set_edge_data(graph):
    """Set lengths, radii, and endefeet_id of edges.

    Args:
        graph (vasculatureAPI.PointVasculature): graph containing point vasculature skeleton.
    """
    lengths, radii, volume = compute_edge_data(graph)
    graph.edge_properties["length"] = lengths
    graph.edge_properties["radius"] = radii
    graph.edge_properties["radius_origin"] = radii
    graph.edge_properties["endfeet_id"] = np.full(radii.shape, -1, dtype=int)
    graph.edge_properties["volume"] = volume


def create_entry_largest_nodes(graph, params):
    """Get largest nodes of degree 1 for input in the largest connected components.

    Args:
        graph (vasculatureAPI.PointVasculature): graph containing point vasculature skeleton.
        params (dict): general parameters for vasculature.

    Returns:
        numpy.array: (1,) Ids of the largest nodes.

    Raises:
        BloodFlowError: if n_nodes <= 0 or if vasc_axis is not 0, 1 or 2.
    """
    if graph is not None:
        if (
            "max_nb_inputs" not in params
            or "depth_ratio" not in params
            or "vasc_axis" not in params
        ):
            raise BloodFlowError("params should contain depth_ratio and max_nb_inputs")
        n_nodes = params["max_nb_inputs"]
        depth_ratio = params["depth_ratio"]
        vasc_axis = params["vasc_axis"]

        if n_nodes < 1:
            raise BloodFlowError("Please provide n_nodes >= 1.")
        if vasc_axis < 0 or vasc_axis > 2:
            raise BloodFlowError("The vasc_axis should be 0, 1 or 2.")
        if depth_ratio < 0:
            depth_ratio = 0.0
            L.warning("The depth_ratio must be >= 0. Taking depth_ratio = 0.")
        if depth_ratio > 1:
            depth_ratio = 1.0
            L.warning("The depth_ratio must be <= 1. Taking depth_ratio = 1.")
        positions = graph.points
        max_position = np.max(positions[:, vasc_axis])
        min_position = np.min(positions[:, vasc_axis])
        depth_max = max_position - (max_position - min_position) * depth_ratio
        degrees = graph.degrees
        sliced_ids = np.where((degrees == 1) & (positions[:, vasc_axis] >= depth_max))[0]
        if sliced_ids.size == 0:
            raise BloodFlowError("Found zero nodes matching our conditions.")
        if sliced_ids.size < n_nodes:
            n_nodes = sliced_ids.size
            L.warning("Too few nodes matching conditions.")
        sliced_index = np.argsort(graph.diameters[sliced_ids])[-n_nodes:]
        return sliced_ids[sliced_index]
    else:
        return None


def get_largest_nodes(graph, n_nodes=1, depth_ratio=1.0, vasc_axis=1):
    """Get largest nodes of degree 1 for input in the largest connected components.

    Args:
        graph (vasculatureAPI.PointVasculature): graph containing point vasculature skeleton.
        n_nodes (int): number of nodes to return
        depth_ratio (float): corresponding to the y-axis (depth of points).
        Portion of the vasculature.
        vasc_axis (int): axis along which we consider the top/bottom of vasculature.

    Returns:
        numpy.array: (1,) Ids of the largest nodes.

    Raises:
        BloodFlowError: if n_nodes <= 0 or if vasc_axis is not 0, 1 or 2.
    """
    if n_nodes <= 0:
        raise BloodFlowError("Please provide n_nodes > 0.")
    if vasc_axis < 0 or vasc_axis > 2:
        raise BloodFlowError("The vasc_axis should be 0, 1 or 2.")
    if depth_ratio < 0:
        depth_ratio = 0.0
        L.warning("The depth_ratio must be >= 0. Taking depth_ratio = 0.")
    if depth_ratio > 1:
        depth_ratio = 1.0
        L.warning("The depth_ratio must be <= 1. Taking depth_ratio = 1.")
    positions = graph.points
    depth_max = (
        np.max(positions[:, vasc_axis])
        - (np.max(positions[:, vasc_axis]) - np.min(positions[:, vasc_axis])) * depth_ratio
    )
    degrees = graph.degrees
    _, labels = sp.sparse.csgraph.connected_components(
        graph.adjacency_matrix.as_sparse(), directed=False, return_labels=True
    )
    largest_cc_label = np.argmax(np.unique(labels, return_counts=True)[1])
    sliced_ids = np.where(
        (degrees == 1) & (labels == largest_cc_label) & (positions[:, vasc_axis] >= depth_max)
    )[0]
    if sliced_ids.size < n_nodes:
        n_nodes = sliced_ids.size
        L.warning("Too few nodes matching conditions.")
    sliced_index = np.argsort(graph.diameters[sliced_ids])[-n_nodes:]
    return sliced_ids[sliced_index]


def get_large_nodes(graph, min_radius=6, depth_ratio=1.0, vasc_axis=1):
    """Get degree 1 nodes that are larger than min_radius.

    Args:
        graph (vasculatureAPI.PointVasculature): graph containing point vasculature skeleton.
        min_radius (float): minimal radius in µm.
        depth_ratio (float): corresponding to the y-axis (depth of points).
        Portion of the vasculature.
        vasc_axis (int): axis along which we consider the top/bottom of vasculature.

    Returns:
        numpy.array: (ids_largest_nodes,) Ids of the largest nodes. Node ids are sorted
        according to their diameters.

    Raises:
        BloodFlowError: if min_radius <= 0 or if vasc_axis is not 0, 1 or 2.
    """
    if min_radius < 0:
        raise BloodFlowError("Please provide min_radius > 0.")
    if vasc_axis < 0 or vasc_axis > 2:
        raise BloodFlowError("The vasc_axis should be 0, 1 or 2.")
    if depth_ratio < 0:
        depth_ratio = 0.0
        L.warning("The depth_ratio must be >= 0. Taking depth_ratio = 0.")
    if depth_ratio > 1:
        depth_ratio = 1.0
        L.warning("The depth_ratio must be <= 1. Taking depth_ratio = 1.")

    positions = graph.points
    depth_max = (
        np.max(positions[:, vasc_axis])
        - (np.max(positions[:, vasc_axis]) - np.min(positions[:, vasc_axis])) * depth_ratio
    )
    degrees = graph.degrees
    _, labels = sp.sparse.csgraph.connected_components(
        graph.adjacency_matrix.as_sparse(), directed=False, return_labels=True
    )
    largest_cc_label = np.argmax(np.unique(labels, return_counts=True)[1])
    sliced_ids = np.where(
        (degrees == 1)
        & (labels == largest_cc_label)
        & (graph.diameters / 2 > min_radius)
        & (positions[:, vasc_axis] >= depth_max)
    )[0]
    sliced_index = np.argsort(graph.diameters[sliced_ids])
    return sliced_ids[sliced_index]


def create_box(graph):  # pragma: no cover
    """Create a box to compute metrics inside.

    Args:
        graph (vasculatureAPI.PointVasculature): graph containing point vasculature skeleton.

    Returns:
        pandas.Series: edge index to filter edges.
    """
    positions = graph.points
    length_box = 400
    vox_x = int(np.max(positions[:, 0])) // 2
    vox_y = int(np.max(positions[:, 1])) // 2
    vox_z = int(np.max(positions[:, 2])) // 2
    origin_box = np.array([vox_x, vox_y, vox_z], dtype=np.uint32)
    mask = (
        (origin_box[0] < graph.node_properties.x)
        & (graph.node_properties.x < origin_box[0] + length_box)
        & (origin_box[1] < graph.node_properties.y)
        & (graph.node_properties.y < origin_box[1] + length_box)
        & (origin_box[2] < graph.node_properties.z)
        & (graph.node_properties.z < origin_box[2] + length_box)
    )
    return graph.edge_properties[
        graph.edge_properties.start_node.isin(graph.node_properties[mask].index)
        & graph.edge_properties.end_node.isin(graph.node_properties[mask].index)
    ].index


def convert_to_networkx(graph):  # pragma: no cover
    """Convert a vasculatureAPI graph to a networkx graph (mostly for plotting purposes).

    Args:
        graph (vasculatureAPI.PointVasculature): graph containing point vasculature skeleton.

    Returns:
        networkx graph: it returns a graph.
    """
    graph_nx = nx.Graph()
    edges = []
    for _, edge in graph.edge_properties.iterrows():
        edges += [
            (
                int(edge["start_node"]),
                int(edge["end_node"]),
                {"length": edge["length"], "radius": edge["radius"]},
            )
        ]
    graph_nx.add_edges_from(edges)

    points = graph.points
    for u in graph_nx:
        graph_nx.nodes[u]["position"] = points[u]

    return graph_nx


def convert_from_networkx(nx_graph):
    """Convert a networkx graph  to a vasculature graph.

    Args:
        nx_graph (networkx graph): input networkx graph

    Returns:
        vascpy.PointVasculature: graph containing point vasculature skeleton.
    """
    node_properties = pd.DataFrame(columns=["x", "y", "z", "diameter"])
    for i in nx_graph:
        node_properties.loc[i, "x"] = nx_graph.nodes[i]["position"][0]
        node_properties.loc[i, "y"] = nx_graph.nodes[i]["position"][1]
        node_properties.loc[i, "z"] = nx_graph.nodes[i]["position"][2]
        node_properties.loc[i, "diameter"] = nx_graph.nodes[i].get("diameter", 1.0)
    node_properties["x"] = pd.to_numeric(node_properties["x"])
    node_properties["y"] = pd.to_numeric(node_properties["y"])
    node_properties["z"] = pd.to_numeric(node_properties["z"])
    node_properties["diameter"] = pd.to_numeric(node_properties["diameter"])

    edge_properties = pd.DataFrame(columns=["start_node", "end_node", "type"])
    for i, (e, v) in enumerate(nx_graph.edges):
        edge_properties.loc[i, "start_node"] = e
        edge_properties.loc[i, "end_node"] = v
        edge_properties.loc[i, "type"] = nx_graph[e][v].get("type", 0)
    edge_properties["start_node"] = pd.to_numeric(edge_properties["start_node"])
    edge_properties["end_node"] = pd.to_numeric(edge_properties["end_node"])
    return PointVasculature(node_properties, edge_properties)


def convert_to_temporal(data, frequencies, times):  # pragma: no cover
    """Convert spectral data to temporal data."""
    data_freq = data.to_numpy()
    computed_frequencies = frequencies[data.columns]
    data_time = pd.DataFrame()
    for loop in times:
        expl = np.exp(2.0j * np.pi * computed_frequencies * loop)
        data_time[loop] = np.real(data_freq.dot(expl)) / len(frequencies)
    return data_time


def is_iterable(v):
    """Check if `v` is any iterable (strings are considered scalar and CircuitNode/EdgeId also)."""
    return isinstance(v, Iterable) and not isinstance(v, str)


def ensure_list(v):
    """Convert iterable / wrap scalar into list (strings are considered scalar)."""
    if is_iterable(v):
        return list(v)
    return [v]


def ensure_ids(a):
    """Convert a numpy array dtype into IDS_DTYPE.

    This function is here due to the https://github.com/numpy/numpy/issues/15084 numpy issue.
    It is quite unsafe to the use uint64 for the ids due to this problem where :
    numpy.uint64 + int --> float64
    numpy.uint64 += int --> float64

    This function needs to be used everywhere node_ids or edge_ids are returned.
    """
    return np.asarray(a, IDS_DTYPE)


class mpi_timer:
    """A simple mpi timer class"""

    _timings = dict()

    @contextmanager
    def region(var):
        start = time.time()
        yield
        elapsed = time.time() - start
        if var in mpi_timer._timings:
            mpi_timer._timings[var][0] += elapsed
            mpi_timer._timings[var][1] += 1
        else:
            mpi_timer._timings[var] = [elapsed, 1]

    @staticmethod
    def print():
        comm = MPI.COMM_WORLD
        nRanks = comm.Get_size()
        myRank = comm.Get_rank()

        # let everyone flush to get a clean output
        sys.stdout.flush()
        comm.Barrier()

        llbl = 0
        for reg in mpi_timer._timings:
            llbl = max(len(reg), llbl)

        if myRank == 0:
            print("")
            print(80 * "-")
            print("Time report [sec]")
            print(80 * "-")
            print(80 * "-")
            print(
                "region",
                (llbl - 6) * " ",
                "max",
                12 * " ",
                "min",
                12 * " ",
                "ave",
                12 * " ",
                "times",
            )
            print(80 * "-")

        for reg in mpi_timer._timings:
            region = np.zeros(1)
            region[0] = mpi_timer._timings[reg][0]
            count = mpi_timer._timings[reg][1]
            region_max = np.zeros(1)
            comm.Reduce(region, region_max, op=MPI.MAX, root=0)
            region_min = np.zeros(1)
            comm.Reduce(region, region_min, op=MPI.MIN, root=0)
            region_ave = np.zeros(1)
            comm.Reduce(region, region_ave, op=MPI.SUM, root=0)
            region_ave /= nRanks

            if myRank == 0:
                s = f'{reg}{(llbl-len(reg))*" "}{region_max[0]:17.3f}'
                s += f"{region_min[0]:17.3f}{region_ave[0]:17.3f}{count:7d}"
                print(s)

        if myRank == 0:
            print(80 * "-")
            print("")


class mpi_mem:
    """A simple mpi memory profiler class"""

    _mem = dict()

    @contextmanager
    def region(var):
        # Memory profiling in MB -> 1024**2
        start = psutil.Process().memory_info().rss / 1024**2
        yield
        elapsed = psutil.Process().memory_info().rss / 1024**2 - start
        if var in mpi_mem._mem:
            mpi_mem._mem[var][0] += elapsed
            mpi_mem._mem[var][1] += 1
        else:
            mpi_mem._mem[var] = [elapsed, 1]

    @staticmethod
    def print():
        comm = MPI.COMM_WORLD
        myRank = comm.Get_rank()

        # let everyone flush to get a clean output
        sys.stdout.flush()
        comm.Barrier()

        llbl = 0
        for reg in mpi_mem._mem:
            llbl = max(len(reg), llbl)

        if myRank == 0:
            print("")
            print(80 * "-")
            print("Memory report [MB]")
            print(80 * "-")
            print(
                "region",
                (llbl - 6) * " ",
                "max",
                12 * " ",
                "min",
                12 * " ",
                "sum",
                12 * " ",
                "times",
            )
            print(80 * "-")

        for reg in mpi_mem._mem:
            region = np.zeros(1)
            region[0] = mpi_mem._mem[reg][0]
            count = mpi_mem._mem[reg][1]
            region_max = np.zeros(1)
            comm.Reduce(region, region_max, op=MPI.MAX, root=0)
            region_min = np.zeros(1)
            comm.Reduce(region, region_min, op=MPI.MIN, root=0)
            region_sum = np.zeros(1)
            comm.Reduce(region, region_sum, op=MPI.SUM, root=0)

            if myRank == 0:
                s = f'{reg}{(llbl-len(reg))*" "}{region_max[0]:17.3f}'
                s += f"{region_min[0]:17.3f}{region_sum[0]:17.3f}{count:7d}"
                print(s)

        if myRank == 0:
            print(80 * "-")
            print("")


def fit_sine_model(signal, window=None):
    """Helper function for fitting a sine signal.

    Computes the parameters [A,f,C] of the model:

        f(t) = A sin( 2 pi f t ) + C

    Args:
        signal (numpy.array): sinusoidal signal.
        window (float): window for smoothing the signal

    Returns:
        A (float): sine amplitude.
        f (float): sine frequency.
        C (float): offset.
    """
    if window is None:
        window = max(len(signal) // 100, 10)  # ad hoc value
    C = np.mean(signal)
    A = np.std(signal) * np.sqrt(2)  # root mean square is a good estimator for the amplitude
    f = len(find_peaks_cwt(signal, widths=[window]))

    msg = "A good practice is to plot the signal and check that the number of peaks corresponds. "
    msg += "If we call f_r the real frequency. The window should be smaller than 0.5*N/f_r. "
    msg += "If the signal domain is not a multiple of a period (2*pi) "
    msg += "the estimation of all parameters can be inaccurate"
    L.warning(msg)
    return A, f, C


def create_input_speed(T, step, A=1, f=1, C=0, read_from_file=None):
    """Creates an input speed v(t) according to the model:

            v(t) = A sin( 2 pi f t ) + C

    Args:
        T (float): simulation time
        step (float): time step size
        A (float): sine amplitude.
        f (float): sine frequency.
        C (float): offset.
        read_from_file (path): path to the data file to read.

    Returns:
        numpy.array: vector of speed for any time point.
    """
    N = round(T / step)  # time steps
    if read_from_file is not None:
        speed = pd.read_csv(read_from_file, header=None)
        if len(speed) < N:
            msg = f"The length of the speed vector {len(speed)} must be greater"
            msg += f" or equal than the number of iterations {N}."
            raise BloodFlowError(msg)
    else:
        time = np.linspace(0, T, N + 1)  # N+1 time points
        speed = C + A * np.sin(2 * np.pi * f * time)

    return speed