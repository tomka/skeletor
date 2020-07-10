#    This script is part of skeletonizer (http://www.github.com/schlegelp/skeletonizer).
#    Copyright (C) 2018 Philipp Schlegel
#    Modified from https://github.com/aalavandhaann/Py_BL_MeshSkeletonization
#    by #0K Srinivasan Ramachandran.
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.


import networkx as nx
import numpy as np
import pandas as pd
import trimesh as tm
import scipy.sparse as sparse
import scipy.spatial

from tqdm.auto import tqdm


def skeletonize(mesh, shape_weight=1, sample_weight=0.1, output='swc',
                progress=True):
    """Skeletonize a (contracted) mesh.

    Important
    ---------
    This implementation is somewhat sensitive to the coordinate space of the
    input mesh: too large and you might experience slow-downs or numpy
    OverflowErrors; too low and you might get skeletons that don't quite match
    the mesh (e.g. too few vertices).

    Parameters
    ----------
    mesh :          trimesh.Trimesh
                    Contracted mesh to be skeletonized.
    shape_weight :  float, optional
                    Weight for shape costs which represent impact of merging
                    two nodes on the shape of the object.
    sample_weight : float, optional
                    Weight for sampling costs which increase if a merge would
                    generate prohibitively long edges.
    output :        "swc" | "graph" | "both"
                    Determines functions output. See Returns.
    progress :      bool
                    If True, will show progress bar.


    Example
    -------
    >>> import trimesh as tm
    >>> m = tm.primitives.Cylinder()

    Returns
    -------
    "swc" :         pandas.DataFrame
                    SWC representation of the skeleton.
    "graph" :       networkx.Graph
                    Graph representation of the skeleton.
    "both" :        tuple
                    Both of the above: ``(swc, graph)``.

    """
    assert isinstance(mesh, tm.Trimesh), f'Expected Trimesh, got "{type(mesh)}"'
    assert output in ['swc', 'graph', 'both']

    # Shorthand faces and edges
    # We convert to arrays to (a) make a copy and (b) remove potential overhead
    # from these originally being trimesh TrackedArrays
    edges = np.array(mesh.edges_unique)
    verts = np.array(mesh.vertices)

    # For cost calculations we will normalise coordinates
    # This prevents getting ridiculuously large cost values ?e300
    # verts = (verts - verts.min()) / (verts.max() - verts.min())
    edge_lengths = np.sqrt(np.sum((verts[edges[:, 0]] - verts[edges[:, 1]])**2, axis=1))

    # Get a list of faces: [(edge1, edge2, edge3), ...]
    face_edges = np.array(mesh.faces_unique_edges)
    # Make sure these faces are unique, i.e. no [(e1, e2, e3), (e3, e2, e1)]
    face_edges = np.unique(np.sort(face_edges, axis=1), axis=0)

    # Shape cost initialisation:
    # Each vertex has a matrix Q which is used to determine the shape cost
    # of collapsing each node. We need to generate a matrix (Q) for each
    # vertex, then when we collapse two nodes, we can update using
    # Qj <- Qi + Qj, so the edges previously associated with vertex i
    # are now associated with vertex j.

    # For each edge, generate a matrix (K). K is made up of two sets of
    # coordinates in 3D space, a and b. a is the normalised edge vector
    # of edge(i,j) and b = a * <x/y/z coordinates of vertex i>
    #
    # The matrix K takes the form:
    #
    #        Kij = 0, -az, ay, -bx
    #              az, 0, -ax, -by
    #             -ay, ax, 0,  -bz

    edge_co0, edge_co1 = verts[edges[:, 0]], verts[edges[:, 1]]
    a = (edge_co1 - edge_co0) / edge_lengths.reshape(edges.shape[0], 1)
    # Note: It's a bit unclear to me whether the normalised edge vector should
    # be allowed to have negative values but I seem to be getting better
    # results if this I use absolut values
    a = np.fabs(a)
    b = a * edge_co0

    # Bunch of zeros
    zero = np.zeros(a.shape[0])

    # Generate matrix K
    K = [[zero,    -a[:, 2], a[:, 1],    -b[:, 0]],
         [a[:, 2],  zero,    -a[:, 0],   -b[:, 1]],
         [-a[:, 1], a[:, 0], zero,       -b[:, 2]]]
    K = np.array(K)

    # Q for vertex i is then the sum of the products of (kT,k) for ALL edges
    # connected to vertex i:
    # Initialize matrix of correct shape
    Q_array = np.zeros((4, 4, verts.shape[0]), dtype=np.float128)

    # Generate (kT, K)
    kT = np.transpose(K, axes=(1, 0, 2))

    # To get the sum of the products in the correct format we have to
    # do some annoying transposes to get to (4, 4, len(edges))
    K_dot = np.matmul(K.T, kT.T).T

    # Iterate over all vertices
    for v in range(len(verts)):
        # Find edges that contain this vertex
        cond1 = edges[:, 0] == v
        cond2 = edges[:, 1] == v
        # Note that this does not take directionality of edges into account
        # Not sure if that's intended?

        # Get indices of these edges
        indices = np.where(cond1 | cond2)[0]

        # Get the products for all edges adjacent to mesh
        Q = K_dot[:, :, indices]
        # Sum over all edges
        Q = Q.sum(axis=2)
        # Add to Q array
        Q_array[:, :, v] = Q

    # Not sure if we are doing something wrong when calculating the Q array but
    # we end up having negative values which translate into negative scores.
    # This in turn is bad because we propagate that negative score when
    # collapsing edges which leads to a "zipper-effect" where nodes collapse
    # in sequence a->b->c->d until they hit some node with really high cost
    # Q_array -= Q_array.min()

    # Edge collapse:
    # Determining which edge to collapse is a weighted sum of the shape and
    # sampling cost. The shape cost of vertex i is Fa(p) = pT Qi p where p is
    # the coordinates of point p (vertex i here) in homogeneous representation.
    # The variable w from above is the value for the homogeneous 4th dimension.
    # T denotes transpose of matrix.
    # The shape cost of collapsing the edge Fa(i,j) = Fi(vj) + Fj(vj).
    # vi and vj being the coordinates of the vertex in homogeneous representation
    # (p in equation before)
    # The sampling cost penalises edge collapses that generate overly long edges,
    # based on the distance traveled by all edges to vi, when vi is merged with
    # vj. (Eq. 7 in paper)
    # You cannot collapse an edge (i -> j) if k is a common adjacent vertex of
    # both i and j, but (i/j/k) is not a face.
    # We will set the cost of these edges to infinity.

    # Now work out the shape cost of collapsing each node (eq. 7)
    # First get coordinates of the first node of each edge
    # Note that in Nik's implementation this was the second node
    p = verts[edges[:, 0]]

    # Append weight factor
    w = 1
    p = np.append(p, np.full((p.shape[0], 1), w), axis=1)

    this_Q1 = Q_array[:, :, edges[:, 0]]
    this_Q2 = Q_array[:, :, edges[:, 1]]

    F1 = np.einsum('ij,kji->ij', p, this_Q1)[:, [0, 1]]
    F2 = np.einsum('ij,kji->ij', p, this_Q2)[:, [0, 1]]

    # Calculate and append shape cost
    F = np.append(F1, F2, axis=1)
    shape_cost = np.sum(F, axis=1)

    # Sum lengths of all edges associated with a given vertex
    # This is easiest by generating a sparse matrix from the edges
    # and then summing by row
    adj = sparse.coo_matrix((edge_lengths,
                             (edges[:, 0], edges[:, 1])),
                            shape=(verts.shape[0], verts.shape[0]))
    adj = adj + adj.T

    # Get the lengths associated with each vertex
    # Note that edges are directional here (which I believe is intended?)
    # i.e. i->k might be 100 but k->i might be 0 (= not set)
    # If not, we could symmetrize the sparse matrix above
    verts_lengths = adj.sum(axis=1)

    # We need to flatten this (something funny with summing sparse matrices)
    verts_lengths = np.array(verts_lengths).flatten()#.astype(np.float64)

    # Map the sum of vertex lengths onto edges (as per first vertex in edge)
    ik_edge = verts_lengths[edges[:, 0]]

    # Calculate sampling cost
    sample_cost = edge_lengths * (ik_edge - edge_lengths)

    # Determine which edge to collapse and collapse it
    # Total Cost - weighted sum of shape and sample cost, equation 8 in paper
    F_T = shape_cost * shape_weight + sample_cost * sample_weight

    # Now start collapsing edges one at a time
    face_count = face_edges.shape[0]  # keep track of face counts for progress bar
    is_collapsed = np.full(edges.shape[0], False)
    keep = np.full(edges.shape[0], False)
    with tqdm(desc='Collapsing faces', total=face_count, disable=progress is False) as pbar:
        while face_edges.size:
            # Uncomment to get a more-or-less random edge collapse
            F_T[:] = 0

            # Update progress bar
            pbar.update(face_count - face_edges.shape[0])
            face_count = face_edges.shape[0]

            # This has to come at the beginning of the loop
            # Set cost of collapsing edges without faces to infinite
            F_T[keep] = np.inf
            F_T[is_collapsed] = np.inf

            # Get the edge that we want to collapse
            collapse_ix = np.argmin(F_T)
            # Get the vertices this edge connects
            u, v = edges[collapse_ix]
            # Get all edges that contain these vertices:
            # First, edges that are (uv, x)
            connects_uv = np.isin(edges[:, 0], [u, v])
            # Second, check if any (uv, x) edges are (uv, uv)
            connects_uv[connects_uv] = np.isin(edges[connects_uv, 1], [u, v])

            # Remove uu and vv edges
            uuvv = edges[:, 0] == edges[:, 1]
            connects_uv = connects_uv & ~uuvv
            # Get the edge's indices
            clps_edges = np.where(connects_uv)[0]

            #print(f'\rFaces {face_edges.shape[0]} | Collapsed {sum(is_collapsed)} | Kept {sum(keep)} | {collapse_ix} \t ({F_T[collapse_ix]:.2f})',
            #      end='\t\t')

            # Now find find the faces the collapsed edge is part of
            # Note: splitting this into three conditions is marginally faster than
            # np.any(np.isin(face_edges, clps_edges), axis=1)
            uv0 = np.isin(face_edges[:, 0], clps_edges)
            uv1 = np.isin(face_edges[:, 1], clps_edges)
            uv2 = np.isin(face_edges[:, 2], clps_edges)
            has_uv = uv0 | uv1 | uv2

            # If these edges do not have adjacent faces anymore
            if not np.any(has_uv):
                # Track this edge as a keeper
                keep[clps_edges] = True
                continue

            # Get the collapsed faces [(e1, e2, e3), ...] for this edge
            clps_faces = face_edges[has_uv]

            # Remove the collapsed faces
            face_edges = face_edges[~has_uv]

            # Track these edges as collapsed
            is_collapsed[clps_edges] = True

            # Get the adjacent edges (i.e. non-uv edges)
            adj_edges = clps_faces[~np.isin(clps_faces, clps_edges)].reshape(clps_faces.shape[0], 2)

            # We have to do some sorting and finding unique edges to make sure
            # remapping is done correctly further down
            # NOTE: Not sure we really need this, so leaving it out for now
            # adj_edges = np.unique(np.sort(adj_edges, axis=1), axis=0)

            # We need to keep track of changes to the adjacent faces
            # Basically each face in (i, j, k) will be reduced to one edge
            # which points from u -> v
            # -> replace occurrences of loosing edge with winning edge
            for win, loose in adj_edges:
                face_edges[face_edges == loose] = win
                is_collapsed[loose] = True

            # Replace occurrences of first node u with second node v
            edges[edges == u] = v

            # Add shape cost of u to shape costs of v
            Q_array[:, :, v] += Q_array[:, :, u]

            # Determine which edges require update of costs:
            # In theory we only need to update costs for edges that are
            # associated with vertices v and u (which now also v)
            has_v = (edges[:, 0] == v) | (edges[:, 1] == v)

            # Uncomment to temporarily force updating costs for all edges
            # has_v[:] = True

            # Update shape costs
            this_Q1 = Q_array[:, :, edges[has_v, 0]]
            this_Q2 = Q_array[:, :, edges[has_v, 1]]

            F1 = np.einsum('ij,kji->ij', p[edges[has_v, 0]], this_Q1)[:, [0, 1]]
            F2 = np.einsum('ij,kji->ij', p[edges[has_v, 1]], this_Q2)[:, [0, 1]]

            F = np.append(F1, F2, axis=1)
            new_shape_cost = np.sum(F, axis=1)

            # Update sum of incoming edge lengths
            # Technically we would have to recalculate lengths of adjacent edges
            # every time but we will take the cheap way out and simply add them up
            verts_lengths[v] += verts_lengths[u]
            # Update sample costs for edges associated with v
            ik_edge = verts_lengths[edges[has_v, 0]]
            new_sample_cost = edge_lengths[has_v] * (ik_edge - edge_lengths[has_v])

            F_T[has_v] = new_shape_cost * shape_weight + new_sample_cost * sample_weight

    # Get the corrected edges
    # corrected_edges = mst_over_mesh(mesh, edges[keep].flatten())
    corrected_edges = edges[keep]

    # Generate graph
    G = edges_to_graph(corrected_edges, mesh.vertices, fix_tree=True, weight=False,
                       drop_disconnected=True)

    if output == 'graph':
        return G

    swc = make_swc(G, mesh)

    if output == 'both':
        return (G, swc)

    return swc


def mst_over_mesh(mesh, verts, limit='auto'):
    """Generate minimum spanning tree by subsetting mesh to given vertices.

    Will (re-)connect vertices based on geodesic distance in original mesh
    using a minimum spanning tree.

    Parameters
    ----------
    mesh :      trimesh.Trimesh
                Mesh to subset.
    verst :     iterable
                Vertex indices to keep for the tree.
    limit :     float | np.inf | "auto"
                Use this to limit the distance for shortest path search
                (``scipy.sparse.csgraph.dijkstra``). Can greatly speed up this
                function at the risk of producing disconnected components. By
                default (auto), we are using 3x the max observed Eucledian
                distance between ``verts``.

    Returns
    -------
    edges :     np.ndarray
                List of `node` -> `parent` edges. Note that these edges are
                already hiearchical, i.e. each node has at exactly 1 parent
                except for the root node(s) which has parent ``-1``.

    """
    # Make sure vertices to keep are unique
    keep = np.unique(verts)

    # Get some shorthands
    verts = mesh.vertices
    edges = mesh.edges_unique
    edge_lengths = mesh.edges_unique_length

    # Produce adjacency matrix from edges and edge lengths
    adj = sparse.coo_matrix((edge_lengths,
                             (edges[:, 0], edges[:, 1])),
                            shape=(verts.shape[0], verts.shape[0]))

    if limit == 'auto':
        distances = scipy.spatial.distance.pdist(verts[keep])
        limit = np.max(distances) * 3

    # Get geodesic distances between vertices
    dist_matrix = sparse.csgraph.dijkstra(csgraph=adj, directed=False,
                                          indices=keep, limit=limit)

    # Subset along second axis
    dist_matrix = dist_matrix[:, keep]

    # Get minimum spanning tree
    mst = sparse.csgraph.minimum_spanning_tree(dist_matrix, overwrite=True)

    # Turn into COO matrix
    coo = mst.tocoo()

    # Extract edge list
    edges = np.array([keep[coo.row], keep[coo.col]]).T

    # Last but not least we have to run a depth first search to turn this
    # into a hierarchical tree, i.e. make edges are orientated in a way that
    # each node only has a single parent (turn a<-b->c into a->b->c)

    # Generate and populate undirected graph
    G = nx.Graph()
    G.add_edges_from(edges)

    # Generate list of parents
    edges = []
    # Go over all connected components
    for c in nx.connected_components(G):
        # Get subgraph of this connected component
        SG = nx.subgraph(G, c)

        # Use first node as root
        r = list(SG.nodes)[0]

        # List of parents: {node: [parent], root: []}
        this_lop = nx.predecessor(SG, r)

        # Note that we assign -1 as root's parent
        edges += [(k, v[0] if v else -1) for k, v in this_lop.items()]

    return np.array(edges).astype(int)


def make_swc(x, mesh, reindex=False, validate=True):
    """Generate SWC table.

    Parameters
    ----------
    x :         numpy.ndarray | networkx.Graph | networkx.DiGraph
                Data to generate SWC from. Can be::

                    - (N, 2) array of child->parent edges
                    - networkX graph

    mesh :      trimesh.Trimesh
                Original mesh. We will extract coordinates.
    reindex :   bool
                If True, will re-number node IDs, if False will keep original
                vertex IDs.
    validate :  bool
                If True, will check check if SWC table is valid and raise an
                exception if issues are found.

    Returns
    -------
    SWC table : pandas.DataFrame

    """
    assert isinstance(mesh, tm.Trimesh)

    if isinstance(x, np.ndarray):
        edges = x
    elif isinstance(x, (nx.Graph, nx.DiGraph)):
        edges = np.array(x.edges)
    else:
        raise TypeError(f'Expected array or Graph, got "{type(x)}"')

    # Make sure edges are unique
    edges = np.unique(edges, axis=0)

    # Generate node table
    swc = pd.DataFrame(edges, columns=['node_id', 'parent_id'])

    # See if we need to add manually add rows for root node(s)
    miss = swc.parent_id.unique()
    miss = miss[~np.isin(miss, swc.node_id.values)]
    if any(miss):
        roots = pd.DataFrame([[n, -1] for n in miss], columns=swc.columns)
        swc = pd.concat([swc, roots], axis=0)

    # Map x/y/z coordinates
    swc['x'] = mesh.vertices[swc.node_id, 0]
    swc['y'] = mesh.vertices[swc.node_id, 1]
    swc['z'] = mesh.vertices[swc.node_id, 2]

    # Placeholder radius
    swc['radius'] = 1

    if reindex:
        # Sort such that the parent is always before the child
        swc.sort_values('parent_id', ascending=True, inplace=True)

        # Reset index
        swc.reset_index(drop=True, inplace=True)

        # Generate mapping
        new_ids = dict(zip(swc.node_id.values, swc.index.values))

        # -1 (root's parent) stays -1
        new_ids[-1] = -1

        swc['node_id'] = swc.node_id.map(new_ids)
        swc['parent_id'] = swc.parent_id.map(new_ids)

    if validate:
        # Check if any node has multiple parents
        if any(swc.node_id.duplicated()):
            raise ValueError('Nodes with multiple parents found.')

    return swc


def edges_to_graph(edges, vertices, fix_tree=True,
                   drop_disconnected=True, weight=True):
    """Create networkx Graph from edge list."""
    # Drop self-loops
    edges = edges[edges[:, 0] != edges[:, 1]]

    # Make sure we don't have a->b and b<-a edges
    edges = edges[~np.any(edges == edges[:, [1, 0]], axis=1)]

    G = nx.Graph()

    nodes = np.unique(edges.flatten())
    coords = vertices[nodes]
    G.add_nodes_from([(e, {'x': co[0], 'y': co[1], 'z': co[2]}) for e, co in zip(nodes, coords)])
    G.add_edges_from([(e[0], e[1]) for e in edges])

    if fix_tree:
        # First remove cycles
        while True:
            try:
                # Find cycle
                cycle = nx.find_cycle(G)
            except nx.exception.NetworkXNoCycle:
                break
            except BaseException:
                raise

            # Sort by degree
            cycle = sorted(cycle, key=lambda x: G.degree[x[0]])

            # Remove the edge with the lowest degree
            G.remove_edge(cycle[0][0], cycle[0][1])

        # Now make sure this is a DAG, i.e. that all edges point in the same direction
        new_edges = []
        for c in nx.connected_components(G.to_undirected()):
            sg = nx.subgraph(G, c)
            # Pick a random root
            r = list(sg.nodes)[0]

            # Generate parent->child dictionary by graph traversal
            this_lop = nx.predecessor(sg, r)

            # Note that we assign -1 as root's parent
            new_edges += [(k, v[0]) for k, v in this_lop.items() if v]

        # We need a directed Graph for this as otherwise the child -> parent
        # order in the edges might get lost
        G = nx.DiGraph()
        G.add_edges_from(new_edges)

    if drop_disconnected:
        # Array of degrees [[node_id, degree], [....], ...]
        deg = np.array(G.degree)
        G.remove_nodes_from(deg[deg[:, 1] == 0][:, 0])

    if weight:
        final_edges = np.array(G.edges)
        vec = vertices[final_edges[:, 0]] - vertices[final_edges[:, 1]]
        weights = np.sqrt(np.sum(vec ** 2, axis=1))
        G.remove_edges_from(list(G.edges))
        G.add_weighted_edges_from([(e[0], e[1], w) for e, w in zip(final_edges, weights)])

    return G


def add_radius(swc, mesh, method='knn', **kwargs):
    """Add/update radius to SWC table.

    Parameters
    ----------
    swc :       pandas.DataFrame
                SWC table.
    mesh :      trimesh.Trimesh
                Mesh to use for radius generation.
    method :    "knn" | "ray" (TODO)
                Whether and how to add radius information to each node::

                    - "knn" uses k-nearest-neighbors to get radii: fast but potential for being very wrong
                    - "ray" uses ray-casting to get radii: slow but "more right"
    **kwargs
                Keyword arguments are passed to the respective method.

    Returns
    -------
    Nothing
                Just adds/updates 'radius' column.

    """
    if method == 'knn':
        swc['radius'] = _get_radius_kkn(swc[['x', 'y', 'z']].values,
                                        mesh=mesh, **kwargs)
    else:
        raise ValueError(f'Unknown method "{method}"')


def _get_radius_kkn(coords, mesh, n=5):
    """Produce radii using k-nearest-neighbors.

    Parameters
    ----------
    coords :    numpy.ndarray
    mesh :      trimesh.Trimesh
    n :         int
                Radius will be the mean over n nearest-neighbors.
    """

    # Generate kdTree
    tree = scipy.spatial.cKDTree(mesh.vertices)

    # Query for coordinates
    dist, ix = tree.query(coords, k=5)

    # We will use the mean but note that outliers might really mess this up
    return np.mean(dist, axis=1)


def edge_in_face(edges, faces):
    """Test if edges are associated with a face. Returns boolean array."""
    # Concatenate edges of all faces (us)
    edges_in_faces = np.concatenate((faces[:,  [0, 1]],
                                     faces[:,  [1, 2]],
                                     faces[:,  [2, 0]]))
    # Since we don't care about the orientation of edges, we just make it so
    # that the lower index is always in the first column
    edges_in_faces = np.sort(edges_in_faces, axis=1)
    edges = np.sort(edges, axis=1)

    # Make unique edges (low ms)
    # - we don't actually need this and it is costly
    # edges_in_faces = np.unique(edges_in_faces, axis=0)

    # Turn face edges into structured array (few us)
    sorted = np.ascontiguousarray(edges_in_faces).view([('', edges_in_faces.dtype)] * edges_in_faces.shape[-1]).ravel()
    # Sort (low ms) -> this is the most costly step at the moment
    sorted.sort(kind='stable')

    # Turn edges into continuous array (few us)
    comp = np.ascontiguousarray(edges).view(sorted.dtype)

    # This asks where elements of "comp" should be inserted which basically
    # tries to align edges and edges_in_faces (tens of ms)
    ind = sorted.searchsorted(comp)

    # If edges are "out of bounds" of the sorted array of face edges the will
    # have "ind = sorted.shape[0] + 1"
    in_bounds = ind < sorted.shape[0]

    # Prepare results (default = False)
    has_face = np.full(edges.shape[0], False, dtype=bool)

    # Check if actually the same for those indices that are within bounds
    has_face[in_bounds.flatten()] = sorted[ind[in_bounds]] == comp[in_bounds]

    return has_face