# Copyright 2024 The Flax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import contextlib
import dataclasses
import functools
import threading
import typing as tp

import jax.experimental

from flax import config
from flax.nnx import filterlib, reprlib, traversals, variablelib
from flax.nnx import statelib
from flax.nnx.proxy_caller import (
  ApplyCaller,
  CallableProxy,
  DelayedAccessor,
)
from flax.nnx.statelib import FlatState, State
from flax.nnx.variablelib import Variable, is_mutable_array
from flax.typing import Key, PathParts, is_key_like
import jax
import numpy as np
import treescope  # type: ignore[import-not-found,import-untyped]
import typing_extensions as tpe

A = tp.TypeVar('A')
B = tp.TypeVar('B')
C = tp.TypeVar('C')
F = tp.TypeVar('F', bound=tp.Callable)

HA = tp.TypeVar('HA', bound=tp.Hashable)
HB = tp.TypeVar('HB', bound=tp.Hashable)
KeyT = tp.TypeVar('KeyT', bound=Key)

Index = int
Names = tp.Sequence[int]
Node = tp.TypeVar('Node')
Leaf = tp.TypeVar('Leaf')
AuxData = tp.TypeVar('AuxData')


@jax.tree_util.register_static
@dataclasses.dataclass(frozen=True, slots=True)
class NoUpdate: ...


NO_UPDATE = NoUpdate()


@jax.tree_util.register_static
@dataclasses.dataclass(frozen=True, slots=True)
class Repeated: ...


REPEATED = Repeated()


@jax.tree_util.register_dataclass
@dataclasses.dataclass(frozen=True, slots=True, repr=False)
class MutableArrayOutput(reprlib.Representable):
  value: jax.Array | NoUpdate | Repeated

  def __nnx_repr__(self):
    yield reprlib.Object(type=type(self))
    yield reprlib.Attr('value', self.value)

  def __treescope_repr__(self, path, subtree_renderer):
    return treescope.repr_lib.render_object_constructor(
      object_type=type(self),
      attributes={
        'value': self.value,
      },
      path=path,
      subtree_renderer=subtree_renderer,
    )


LeafType = tp.Union[
  Variable,
  jax.Array,
  np.ndarray,
  variablelib.MutableArray,
  MutableArrayOutput,
  NoUpdate,
]
GraphState = State[Key, LeafType]
GraphFlatState = FlatState[LeafType]


def is_node_leaf(x: tp.Any) -> tpe.TypeGuard[LeafType]:
  return isinstance(x, LeafType) or variablelib.is_mutable_array(x)  # type: ignore[misc, arg-type]


class IndexMap(dict[Index, tp.Any]):
  @staticmethod
  def from_refmap(refmap: RefMap) -> IndexMap:
    return IndexMap((index, value) for value, index in refmap.items())


if config.flax_use_flaxlib:
  import flaxlib  # type: ignore[import]

  globals()['IndexMap'] = flaxlib.IndexMap


# RefMap = dict
class RefMap(tp.MutableMapping[tp.Any, int], reprlib.MappingReprMixin):
  """A mapping that hashes keys by their identity."""

  def __init__(
    self,
    mapping: tp.Mapping[tp.Any, int]
    | tp.Iterable[tuple[tp.Any, int]]
    | None = None,
    /,
  ):
    self._mapping: dict[int, tuple[tp.Any, int]] = dict()
    if mapping is not None:
      self.update(mapping)

  @staticmethod
  def from_indexmap(indexmap: IndexMap) -> RefMap:
    refmap = RefMap()
    refmap.update((value, index) for index, value in indexmap.items())
    return refmap

  def get(self, key: tp.Any, default: int | None = None) -> int | None:  # type: ignore[override]
    return self._mapping.get(id(key), (None, default))[1]

  def __getitem__(self, key: tp.Any) -> int:
    return self._mapping[id(key)][1]

  def __setitem__(self, key: tp.Any, value: int):
    self._mapping[id(key)] = (key, value)

  def __delitem__(self, key: tp.Any):
    del self._mapping[id(key)]

  def __len__(self) -> int:
    return len(self._mapping)

  def __contains__(self, key: tp.Any) -> bool:
    return id(key) in self._mapping

  def __iter__(self) -> tp.Iterator[tp.Any]:
    for key, _ in self._mapping.values():
      yield key

  def items(self) -> tp.ItemsView[tp.Any, int]:
    return self._mapping.values()  # type: ignore


# save python version
PythonRefMap = RefMap

if config.flax_use_flaxlib:
  import flaxlib  # type: ignore[import]

  globals()['RefMap'] = flaxlib.RefMap


@dataclasses.dataclass(frozen=True, slots=True)
class NodeImplBase(tp.Generic[Node, Leaf, AuxData]):
  type: type[Node]
  flatten: tp.Callable[[Node], tuple[tp.Sequence[tuple[Key, Leaf]], AuxData]]

  def node_dict(self, node: Node) -> dict[Key, Leaf]:
    nodes, _ = self.flatten(node)
    return dict(nodes)


@dataclasses.dataclass(frozen=True, slots=True)
class GraphNodeImpl(NodeImplBase[Node, Leaf, AuxData]):
  set_key: tp.Callable[[Node, Key, Leaf], None]
  pop_key: tp.Callable[[Node, Key], Leaf]
  create_empty: tp.Callable[[AuxData], Node]
  clear: tp.Callable[[Node], None]
  init: tp.Callable[[Node, tp.Iterable[tuple[Key, Leaf]]], None]


@dataclasses.dataclass(frozen=True, slots=True)
class PytreeNodeImpl(NodeImplBase[Node, Leaf, AuxData]):
  unflatten: tp.Callable[[tp.Sequence[tuple[Key, Leaf]], AuxData], Node]


NodeImpl = tp.Union[
  GraphNodeImpl[Node, Leaf, AuxData], PytreeNodeImpl[Node, Leaf, AuxData]
]


GRAPH_REGISTRY: dict[type, NodeImpl[tp.Any, tp.Any, tp.Any]] = {}
PYTREE_REGISTRY: dict[type, PytreeNodeImpl[tp.Any, tp.Any, tp.Any]] = {}


def register_graph_node_type(
  type: type,
  flatten: tp.Callable[[Node], tuple[tp.Sequence[tuple[Key, Leaf]], AuxData]],
  set_key: tp.Callable[[Node, Key, Leaf], None],
  pop_key: tp.Callable[[Node, Key], Leaf],
  create_empty: tp.Callable[[AuxData], Node],
  clear: tp.Callable[[Node], None],
  init: tp.Callable[[Node, tp.Iterable[tuple[Key, Leaf]]], None],
):
  if type in GRAPH_REGISTRY:
    raise ValueError(f'Node type {type} is already registered.')

  GRAPH_REGISTRY[type] = GraphNodeImpl(
    type=type,
    flatten=flatten,
    set_key=set_key,
    pop_key=pop_key,
    create_empty=create_empty,
    clear=clear,
    init=init,
  )


def register_pytree_node_type(
  type: type,
  flatten: tp.Callable[[Node], tuple[tp.Sequence[tuple[Key, Leaf]], AuxData]],
  unflatten: tp.Callable[[tp.Sequence[tuple[Key, Leaf]], AuxData], Node],
):
  if type in PYTREE_REGISTRY:
    raise ValueError(f'Node type {type} is already registered.')

  PYTREE_REGISTRY[type] = PytreeNodeImpl(
    type=type, flatten=flatten, unflatten=unflatten
  )


def is_node(x: tp.Any) -> bool:
  if isinstance(x, Variable):
    return False
  if type(x) in GRAPH_REGISTRY:
    return True
  return is_pytree_node(x)


def is_graph_node(x: tp.Any) -> bool:
  return type(x) in GRAPH_REGISTRY or variablelib.is_mutable_array(x)


def is_node_type(x: type[tp.Any]) -> bool:
  return x in GRAPH_REGISTRY or x in PYTREE_REGISTRY or x is GenericPytree


def get_node_impl(x: Node) -> NodeImpl[Node, tp.Any, tp.Any] | None:
  if isinstance(x, Variable):
    return None

  node_type = type(x)

  if node_type in GRAPH_REGISTRY:
    return GRAPH_REGISTRY[node_type]
  elif node_type in PYTREE_REGISTRY:
    return PYTREE_REGISTRY[node_type]
  elif node_type in JAX_PYTREE_REGISTRY or issubclass(node_type, tuple):
    return PYTREE_NODE_IMPL  # type: ignore
  else:
    return None


def get_node_impl_for_type(
  x: type[Node],
) -> NodeImpl[Node, tp.Any, tp.Any] | None:
  if x is GenericPytree:
    return PYTREE_NODE_IMPL  # type: ignore
  elif x in PYTREE_REGISTRY:
    return PYTREE_REGISTRY[x]
  elif x in GRAPH_REGISTRY:
    return GRAPH_REGISTRY[x]
  else:
    return None


class HashableMapping(tp.Mapping[HA, HB], tp.Hashable):
  _mapping: dict[HA, HB] | tp.Mapping[HA, HB]

  def __init__(self, mapping: tp.Mapping[HA, HB], copy: bool = True):
    self._mapping = dict(mapping) if copy else mapping

  def __contains__(self, key: object) -> bool:
    return key in self._mapping

  def __getitem__(self, key: HA) -> HB:
    return self._mapping[key]

  def __iter__(self) -> tp.Iterator[HA]:
    return iter(self._mapping)

  def __len__(self) -> int:
    return len(self._mapping)

  def __hash__(self) -> int:
    return hash(tuple(sorted(self._mapping.items())))

  def __eq__(self, other: tp.Any) -> bool:
    return (
      isinstance(other, HashableMapping) and self._mapping == other._mapping
    )

  def __repr__(self) -> str:
    return repr(self._mapping)


@jax.tree_util.register_static
@dataclasses.dataclass(frozen=True, repr=False)
class NodeRef(tp.Generic[Node], reprlib.Representable):
  index: int

  def __nnx_repr__(self):
    yield reprlib.Object(type=type(self))
    yield reprlib.Attr('index', self.index)

  def __treescope_repr__(self, path, subtree_renderer):
    return treescope.repr_lib.render_object_constructor(
      object_type=type(self),
      attributes={'index': self.index},
      path=path,
      subtree_renderer=subtree_renderer,
    )


if config.flax_use_flaxlib:
  import flaxlib  # type: ignore[import]

  jax.tree_util.register_static(flaxlib.NodeRef)
  globals()['NodeRef'] = flaxlib.NodeRef


@dataclasses.dataclass(frozen=True, repr=False)
class VariableDef(reprlib.Representable, tp.Generic[Node]):
  type: type[Node]
  index: int
  outer_index: int | None
  metadata: HashableMapping[str, tp.Any]
  mutable_arraydef: MutableArrayDef | NodeRef | None

  def with_no_outer_index(self) -> VariableDef:
    return VariableDef(
      type=self.type,
      index=self.index,
      outer_index=None,
      metadata=self.metadata,
      mutable_arraydef=self.mutable_arraydef.with_no_outer_index()
      if isinstance(self.mutable_arraydef, MutableArrayDef)
      else self.mutable_arraydef,
    )

  def with_same_outer_index(self) -> VariableDef:
    return VariableDef(
      type=self.type,
      index=self.index,
      outer_index=self.index,
      metadata=self.metadata,
      mutable_arraydef=self.mutable_arraydef.with_same_outer_index()
      if isinstance(self.mutable_arraydef, MutableArrayDef)
      else self.mutable_arraydef,
    )

  def __nnx_repr__(self):
    yield reprlib.Object(type=type(self))
    yield reprlib.Attr('type', self.type.__name__)
    yield reprlib.Attr('index', self.index)
    yield reprlib.Attr('outer_index', self.outer_index)
    yield reprlib.Attr('metadata', reprlib.PrettyMapping(self.metadata))

  def __treescope_repr__(self, path, subtree_renderer):
    return treescope.repr_lib.render_object_constructor(
      object_type=type(self),
      attributes={
        'type': self.type,
        'index': self.index,
        'outer_index': self.outer_index,
        'metadata': self.metadata,
      },
      path=path,
      subtree_renderer=subtree_renderer,
    )


if config.flax_use_flaxlib:
  import flaxlib  # type: ignore[import]

  jax.tree_util.register_static(flaxlib.VariableDef)
  globals()['VariableDef'] = flaxlib.VariableDef


@dataclasses.dataclass(frozen=True, repr=False)
class MutableArrayDef(reprlib.Representable):
  index: int
  outer_index: int | None

  def with_no_outer_index(self):
    return MutableArrayDef(
      index=self.index,
      outer_index=None,
    )

  def with_same_outer_index(self):
    return MutableArrayDef(
      index=self.index,
      outer_index=self.index,
    )

  def __nnx_repr__(self):
    yield reprlib.Object(type=type(self))
    yield reprlib.Attr('index', self.index)
    yield reprlib.Attr('outer_index', self.outer_index)

  def __treescope_repr__(self, path, subtree_renderer):
    return treescope.repr_lib.render_object_constructor(
      object_type=type(self),
      attributes={
        'index': self.index,
        'outer_index': self.outer_index,
      },
      path=path,
      subtree_renderer=subtree_renderer,
    )


@jax.tree_util.register_static
@dataclasses.dataclass(frozen=True, repr=False, slots=True)
class NodeDef(tp.Generic[Node], reprlib.Representable):
  """A dataclass that denotes the tree structure of a
  :class:`Module`. A ``GraphDef`` can be generated by either
  calling :func:`split` or :func:`graphdef` on the :class:`Module`."""

  type: tp.Type[Node]
  index: int | None
  outer_index: int | None
  num_attributes: int
  metadata: tp.Any

  def with_no_outer_index(self) -> NodeDef[Node]:
    return NodeDef(
      type=self.type,
      index=self.index,
      outer_index=None,
      num_attributes=self.num_attributes,
      metadata=self.metadata,
    )

  def with_same_outer_index(self) -> NodeDef[Node]:
    return NodeDef(
      type=self.type,
      index=self.index,
      outer_index=self.index,
      num_attributes=self.num_attributes,
      metadata=self.metadata,
    )

  def __nnx_repr__(self):
    yield reprlib.Object(type=type(self))

    yield reprlib.Attr('type', self.type.__name__)
    yield reprlib.Attr('index', self.index)
    yield reprlib.Attr('outer_index', self.outer_index)
    yield reprlib.Attr('num_attributes', self.num_attributes)
    yield reprlib.Attr('metadata', self.metadata)

  def __treescope_repr__(self, path, subtree_renderer):
    return treescope.repr_lib.render_object_constructor(
      object_type=type(self),
      attributes={
        'type': self.type,
        'index': self.index,
        'outer_index': self.outer_index,
        'num_attributes': self.num_attributes,
        'metadata': self.metadata,
      },
      path=path,
      subtree_renderer=subtree_renderer,
    )


if config.flax_use_flaxlib:
  import flaxlib  # type: ignore[import]

  jax.tree_util.register_static(flaxlib.NodeDef)
  globals()['NodeDef'] = flaxlib.NodeDef

NodeDefType = tp.Union[
  NodeDef[Node],
  NodeRef[Node],
  VariableDef[Node],
  MutableArrayDef,
]


@dataclasses.dataclass(frozen=True, slots=True)
class ArrayAttr:
  pass


ARRAY_ATTR = ArrayAttr()


@dataclasses.dataclass(frozen=True, slots=True)
class MutableArrayAttr:
  pass


MUTABLE_ARRAY_ATTR = MutableArrayAttr()


@dataclasses.dataclass(frozen=True, slots=True)
class NodeAttr:
  pass


NODE_ATTR = NodeAttr()

AttrType = tp.Union[
  NodeAttr,
  ArrayAttr,
  MutableArrayAttr,
  'Static[tp.Any]',
]


# GraphDef = tp.Union[NodeDef[Node], NodeRef[Node], VariableDef[Node]]
@jax.tree_util.register_static
@dataclasses.dataclass(frozen=True, slots=True)
class GraphDef(tp.Generic[Node]):
  nodes: list[NodeDefType[tp.Any]]
  attributes: list[tuple[Key, AttrType]]
  num_leaves: int

  def __hash__(self) -> int:
    return hash((tuple(self.nodes), tuple(self.attributes)))

  def with_no_outer_index(self) -> GraphDef[Node]:
    return GraphDef(
      nodes=[
        node.with_no_outer_index() if not isinstance(node, NodeRef) else node
        for node in self.nodes
      ],
      attributes=self.attributes,
      num_leaves=self.num_leaves,
    )

  def with_same_outer_index(self) -> GraphDef[Node]:
    return GraphDef(
      nodes=[
        node.with_same_outer_index() if not isinstance(node, NodeRef) else node
        for node in self.nodes
      ],
      attributes=self.attributes,
      num_leaves=self.num_leaves,
    )

  # TODO(cgarciae): remove this method
  def apply(
    self, state: GraphState, *states: GraphState
  ) -> ApplyCaller[tuple[GraphDef[Node], GraphState]]:
    accessor = DelayedAccessor()

    def _apply(
      accessor: DelayedAccessor, *args, **kwargs
    ) -> tuple[tp.Any, tuple[GraphDef[Node], GraphState]]:
      module = merge(self, state, *states)
      fn = accessor(module)
      out = fn(*args, **kwargs)
      graphdef, flat_state = flatten(module)
      state_ = statelib.from_flat_state(flat_state)
      return out, (graphdef, state_)

    return CallableProxy(_apply, accessor)  # type: ignore


PureState = tuple[GraphDef[Node], GraphState]


@tp.overload
def flatten(  # type: ignore[invalid-annotation]
  node: Node,
  /,
  *,
  ref_index: RefMap | None = None,
  ref_outer_index: RefMap | None = None,
) -> tuple[GraphDef[Node], FlatState[tp.Any]]: ...
@tp.overload
def flatten(  # type: ignore[invalid-annotation]
  node: Node,
  /,
  *,
  with_paths: tp.Literal[True],
  ref_index: RefMap | None = None,
  ref_outer_index: RefMap | None = None,
) -> tuple[
  GraphDef[Node],
  FlatState[tp.Any],
]: ...
@tp.overload
def flatten(  # type: ignore[invalid-annotation]
  node: Node,
  /,
  *,
  with_paths: tp.Literal[False],
  ref_index: RefMap | None = None,
  ref_outer_index: RefMap | None = None,
) -> tuple[
  GraphDef[Node],
  list[tp.Any],
]: ...
@tp.overload
def flatten(  # type: ignore[invalid-annotation]
  node: Node,
  /,
  *,
  with_paths: bool,
  ref_index: RefMap | None = None,
  ref_outer_index: RefMap | None = None,
) -> tuple[
  GraphDef[Node],
  FlatState[tp.Any] | list[tp.Any],
]: ...
def flatten(  # type: ignore[invalid-annotation]
  node: Node,
  /,
  *,
  with_paths: bool = True,
  ref_index: RefMap | None = None,
  ref_outer_index: RefMap | None = None,
) -> tuple[
  GraphDef[Node],
  FlatState[tp.Any] | list[tp.Any],
]:
  """Flattens a graph node into a (graphdef, state) pair.

  Args:
    x: A graph node.
    ref_index: A mapping from nodes to indexes, defaults to None. If not provided, a new
      empty dictionary is created. This argument can be used to flatten a sequence of graph
      nodes that share references.
    with_paths: A boolean that indicates whether to return a FlatState object that includes
      the paths, or just a list of the Variable's inner values.
  """
  if ref_index is None:
    ref_index = RefMap()

  leaves: list[tp.Any] = []
  path: list[Key] | None = [] if with_paths else None
  paths: list[PathParts] | None = [] if with_paths else None
  nodes: list[NodeDefType[tp.Any]] = []
  attributes: list[tuple[Key, AttrType]] = []
  node_impl = get_node_impl(node)
  _graph_flatten(
    node,
    node_impl,
    path,
    ref_index,
    ref_outer_index,
    nodes,
    attributes,
    leaves,
    paths,
  )
  graphdef: GraphDef = GraphDef(
    nodes=nodes, attributes=attributes, num_leaves=len(leaves)
  )

  if paths is not None:
    return graphdef, FlatState.from_sorted_keys_values(tuple(paths), leaves)  # type: ignore[return-value]
  else:
    return graphdef, leaves


def _graph_flatten(
  node: Node,
  node_impl: NodeImpl[Node, Leaf, AuxData] | None,
  path: list[Key] | None,
  ref_index: RefMap,
  ref_outer_index: RefMap | None,
  nodes: list[NodeDefType[tp.Any]],
  attributes: list[tuple[Key, AttrType]],
  leaves: list[tp.Any],
  paths: list[PathParts] | None,
) -> None:
  is_pytree_node_ = type(node_impl) is PytreeNodeImpl

  index: int | None
  if not is_pytree_node_ and node in ref_index:
    nodes.append(NodeRef(index := ref_index[node]))
    return

  is_graph_node_ = type(node_impl) is GraphNodeImpl
  is_variable = isinstance(node, Variable)
  is_mutable_array = variablelib.is_mutable_array(node)

  # only cache graph nodes, we don't add mutable arrays here
  # as they are added in the make_mutable_arraydef function
  if is_graph_node_ or is_variable:
    index = len(ref_index)
    ref_index[node] = index
  else:
    index = None

  def make_mutable_arraydef(value: variablelib.MutableArray):
    if value in ref_index:
      index = ref_index[value]
      return NodeRef(index), REPEATED
    else:
      index = len(ref_index)
      ref_index[value] = index
    output_value: NoUpdate | MutableArrayOutput | variablelib.MutableArray
    if ref_outer_index is not None:
      if value in ref_outer_index:
        outer_index = ref_outer_index[value]
        output_value = NO_UPDATE
        mutable_arraydef = MutableArrayDef(index=index, outer_index=outer_index)
      else:
        output_value = MutableArrayOutput(value[...])
        mutable_arraydef = MutableArrayDef(index=index, outer_index=None)
    else:
      output_value = value
      mutable_arraydef = MutableArrayDef(index=index, outer_index=None)
    return mutable_arraydef, output_value

  if is_variable:
    assert isinstance(node, Variable)
    assert index is not None
    prev_inner_value = node.raw_value
    if variablelib.is_mutable_array(prev_inner_value):
      mutable_arraydef, inner_value = make_mutable_arraydef(prev_inner_value)
    else:
      mutable_arraydef = None
      inner_value = prev_inner_value
    if path is None:
      leaf = inner_value
    else:
      leaf = node  # type: ignore[assignment]
      if inner_value is not prev_inner_value:
        leaf.raw_value = inner_value

    variabledef = VariableDef(
      type=type(node),
      index=index,
      outer_index=ref_outer_index.get(node, None) if ref_outer_index else None,
      metadata=HashableMapping(node._var_metadata),
      mutable_arraydef=mutable_arraydef,
    )
    if type(inner_value) is not Repeated:
      assert not isinstance(leaf, Repeated)
      leaves.append(leaf)
      if path is not None:
        assert paths is not None
        paths.append(tuple(path))
    nodes.append(variabledef)
    return
  elif is_mutable_array:
    mutable_arraydef, leaf = make_mutable_arraydef(node)  # type: ignore[arg-type]
    if not isinstance(leaf, Repeated):
      leaves.append(leaf)
      if path is not None:
        assert paths is not None
        paths.append(tuple(path))
    nodes.append(mutable_arraydef)
    return
  elif not is_pytree_node_ and not is_graph_node_:
    # unkown leaf
    leaves.append(node)
    if path is not None:
      assert paths is not None
      paths.append(tuple(path))
    return

  if node_impl is None:
    raise RuntimeError(f'Unsupported type: {type(node)}, this is a bug.')

  values, metadata = node_impl.flatten(node)
  num_attributes = len(values)
  nodedef = NodeDef(
    node_impl.type,
    index,
    ref_outer_index[node]
    if is_graph_node_ and ref_outer_index and node in ref_outer_index
    else None,
    num_attributes,
    metadata,
  )
  nodes.append(nodedef)

  for key, value in values:
    value_node_impl = get_node_impl(value)
    if path is not None:
      path.append(key)
    if value_node_impl is not None or isinstance(value, Variable):
      attributes.append((key, NODE_ATTR))
      _graph_flatten(
        value,
        value_node_impl,
        path,
        ref_index,
        ref_outer_index,
        nodes,
        attributes,
        leaves,
        paths,
      )
    elif variablelib.is_mutable_array(value):
      attributes.append((key, MUTABLE_ARRAY_ATTR))
      mutable_arraydef, leaf = make_mutable_arraydef(value)
      if not isinstance(leaf, Repeated):
        leaves.append(leaf)
        if paths is not None:
          paths.append(tuple(path))  # type: ignore
      nodes.append(mutable_arraydef)
    elif isinstance(value, (jax.Array, np.ndarray)):
      attributes.append((key, ARRAY_ATTR))
      if paths is not None:
        paths.append(tuple(path))  # type: ignore
      leaves.append(value)
    else:
      attributes.append((key, Static(value)))

    if path is not None:
      path.pop()

  return


@dataclasses.dataclass(slots=True)
class FingerprintContext:
  next_index: int


# TODO(cgarciae): the actual fingerprint object is not being used,
# only the traversal process is still relevant
def fingerprint(
  node,
  /,
  *,
  ref_index: RefMap | None = None,
  new_ref_index: RefMap | None = None,
) -> list[tp.Hashable]:
  """ """
  if ref_index is None:
    ref_index = RefMap()

  if new_ref_index is None:
    new_ref_index = RefMap()
  node_impl = get_node_impl(node)
  if node_impl is None:
    raise RuntimeError(f'Unsupported type: {type(node)}, this is a bug.')
  ctx = FingerprintContext(len(ref_index) + len(new_ref_index))
  fp: list[tp.Hashable] = []
  _graph_fingerprint(ctx, fp.append, node, node_impl, ref_index, new_ref_index)
  return fp


def _graph_fingerprint(
  ctx: FingerprintContext,
  append_fn: tp.Callable[[tp.Any], None],
  node,
  node_impl: NodeImpl[Node, Leaf, AuxData],
  ref_index: RefMap,
  new_ref_index: RefMap,
):
  is_pytree_node_ = type(node_impl) is PytreeNodeImpl
  is_graph_node_ = type(node_impl) is GraphNodeImpl

  append_fn(type(node))

  if is_graph_node_:
    append_fn(id(node))
    if node in ref_index:
      append_fn(ref_index[node])
      return
    elif node in new_ref_index:
      append_fn(new_ref_index[node])
      return
    index = new_ref_index[node] = ctx.next_index
    ctx.next_index += 1
  else:
    index = -1

  values, metadata = node_impl.flatten(node)

  append_fn(index)
  append_fn(metadata)

  for key, value in values:
    value_node_impl = get_node_impl(value)
    append_fn(key)
    if value_node_impl is not None:
      _graph_fingerprint(
        ctx,
        append_fn,
        value,
        value_node_impl,
        ref_index,
        new_ref_index,
      )
    elif isinstance(value, Variable):
      append_fn(id(value))
      append_fn(type(value))
      if value in ref_index:
        append_fn(ref_index[value])
      elif value in new_ref_index:
        append_fn(new_ref_index[value])
      else:
        variable_index = new_ref_index[value] = ctx.next_index
        ctx.next_index += 1
        append_fn(variable_index)
        for key_value in value._var_metadata.items():
          append_fn(key_value)
    elif not isinstance(value, (jax.Array, np.ndarray)):
      append_fn(value)


def check_fingerprint(
  node,
  fp: list[tp.Hashable],
  /,
  *,
  ref_index: RefMap | None = None,
  new_ref_index: RefMap | None = None,
) -> bool:
  """ """
  if ref_index is None:
    ref_index = RefMap()

  if new_ref_index is None:
    new_ref_index = RefMap()
  node_impl = get_node_impl(node)
  if node_impl is None:
    raise RuntimeError(f'Unsupported type: {type(node)}, this is a bug.')
  ctx = FingerprintContext(len(ref_index) + len(new_ref_index))
  fp_matches = _check_graph_fingerprint(
    ctx, iter(fp), node, node_impl, ref_index, new_ref_index
  )
  return fp_matches


def _check_graph_fingerprint(
  ctx: FingerprintContext,
  fp_iterator: tp.Iterator[tp.Hashable],
  node,
  node_impl: NodeImpl[Node, Leaf, AuxData],
  ref_index: RefMap,
  new_ref_index: RefMap,
) -> bool:
  is_pytree_node_ = type(node_impl) is PytreeNodeImpl
  is_graph_node_ = type(node_impl) is GraphNodeImpl

  if type(node) != next(fp_iterator):
    return False

  if is_graph_node_:
    # append_fn(id(node))
    if id(node) != next(fp_iterator):
      return False
    if node in ref_index:
      # append_fn(ref_index[node])
      return ref_index[node] == next(fp_iterator)
    elif node in new_ref_index:
      # append_fn(new_ref_index[node])
      return new_ref_index[node] == next(fp_iterator)
    index = new_ref_index[node] = ctx.next_index
    ctx.next_index += 1
  else:
    index = -1

  values, metadata = node_impl.flatten(node)

  # append_fn(index)
  if index != next(fp_iterator):
    return False
  # append_fn(metadata)
  if metadata != next(fp_iterator):
    return False

  for key, value in values:
    value_node_impl = get_node_impl(value)
    # append_fn(key)
    if key != next(fp_iterator):
      return False
    if value_node_impl is not None:
      if not _check_graph_fingerprint(
        ctx,
        fp_iterator,
        value,
        value_node_impl,
        ref_index,
        new_ref_index,
      ):
        return False
    elif isinstance(value, Variable):
      # append_fn(id(value))
      if id(value) != next(fp_iterator):
        return False
      # append_fn(type(value))
      if type(value) != next(fp_iterator):
        return False
      if value in ref_index:
        # append_fn(ref_index[value])
        if ref_index[value] != next(fp_iterator):
          return False
      elif value in new_ref_index:
        # append_fn(new_ref_index[value])
        if new_ref_index[value] != next(fp_iterator):
          return False
      else:
        variable_index = new_ref_index[value] = ctx.next_index
        ctx.next_index += 1
        # append_fn(variable_index)
        if variable_index != next(fp_iterator):
          return False
        for key_value in value._var_metadata.items():
          # append_fn(key_value)
          if key_value != next(fp_iterator):
            return False
    else:
      if isinstance(value, (jax.Array, np.ndarray)):
        raise ValueError(f'Arrays leaves are not supported: {value}')
      # append_fn(value)
      if value != next(fp_iterator):
        return False

  return True


def _get_sorted_leaves(
  xs: tp.Mapping[tp.Any, tp.Any],
) -> list[tp.Any]:
  if not isinstance(xs, tp.Mapping):  # type: ignore
    raise TypeError(f'expected Mapping; got {type(xs).__qualname__}')
  leaves: list[tp.Any] = []

  def _flatten(xs):
    if not isinstance(xs, tp.Mapping):
      leaves.append(xs)
    else:
      for _, value in sorted(xs.items()):
        _flatten(value)

  _flatten(xs)
  return leaves


def unflatten(  # type: ignore[invalid-annotation]
  graphdef: GraphDef[Node],
  state: State[Key, tp.Any] | FlatState[tp.Any] | list[tp.Any],
  /,
  *,
  index_ref: IndexMap | None = None,
  outer_index_outer_ref: IndexMap | None = None,
  copy_variables: bool = True,
) -> Node:
  """Unflattens a graphdef into a node with the given state.

  Args:
    graphdef: A GraphDef instance.
    state: A State instance.
    index_ref: A mapping from indexes to nodes references found during the graph
      traversal, defaults to None. If not provided, a new empty dictionary is
      created. This argument can be used to unflatten a sequence of (graphdef, state)
      pairs that share the same index space.
    index_ref_cache: A mapping from indexes to existing nodes that can be reused.
      When an reference is reused, ``GraphNodeImpl.clear`` is called to leave the
      object in an empty state and then filled by the unflatten process, as a result
      existing graph nodes are mutated to have the new content/topology
      specified by the graphdef.
    copy_variables: If True (default), variables in the state will be copied onto
      the new new structure, else variables will be shared.
  """
  if isinstance(state, (State, dict)):
    leaves = _get_sorted_leaves(state)
  elif isinstance(state, FlatState):
    leaves = state.leaves
  elif isinstance(state, list):  # type: ignore
    leaves = state
  else:
    raise ValueError(f'Unsupported state type: {type(state)}')
  if index_ref is None:
    index_ref = IndexMap()

  if len(leaves) != graphdef.num_leaves:
    raise ValueError(
      f'Incorrect number of leaves, expected {graphdef.num_leaves} leaves, but got {len(leaves)}.'
    )

  if len(graphdef.nodes) == 0:
    # unkown leaf
    return leaves[0]
  elif isinstance(nodedef := graphdef.nodes[0], NodeRef):
    node = index_ref[nodedef.index]
  else:
    node_iter = iter(graphdef.nodes)
    attribute_iter = iter(graphdef.attributes)
    leaves_iter = iter(leaves)
    nodedef = next(node_iter)
    assert not isinstance(nodedef, NodeRef)
    if isinstance(nodedef, MutableArrayDef):
      node_impl = None
    else:
      node_impl = get_node_impl_for_type(nodedef.type)
    node = _graph_unflatten(
      nodedef,
      node_impl,
      node_iter,
      attribute_iter,
      leaves_iter,
      index_ref,
      outer_index_outer_ref,
      copy_variables,
    )

    try:
      next(leaves_iter)
    except StopIteration:
      pass
    else:
      raise ValueError('Incorrect number of leaves in state.')

  return node


def _graph_unflatten(
  nodedef: NodeDefType[Node],
  node_impl: NodeImpl[Node, Leaf, AuxData] | None,
  node_iter: tp.Iterator[NodeDefType[Node]],
  attribute_iter: tp.Iterator[tuple[Key, AttrType]],
  leaves_iter: tp.Iterator[tp.Any],
  index_ref: IndexMap,
  outer_index_outer_ref: IndexMap | None,
  copy_variables: bool,
) -> Node:
  """Recursive helper for graph_unflatten.

  Args:
    nodedef: A GraphDef instance or an index to a node in the cache.
    state: A mapping from attribute names to variables or subgraphs.
    index_to_ref: A mapping from indexes to nodes that have been traversed.
      If a node is already in the cache, it won't be traversed again.
f0f6619b-dde6-4466-b699-61c47f268d6b    index_ref_cache: A mapping from indexes to existing nodes that can be reused.
      When an reference is reused, ``GraphNodeImpl.clear`` is called to leave the
      object in an empty state and then filled by the unflatten process, as a result
      existing graph nodes are mutated to have the new content/topology
      specified by the nodedef.
  """

  def get_mutable_array(mutable_arraydef: MutableArrayDef, leaf):
    assert type(mutable_arraydef) is MutableArrayDef
    if (
      outer_index_outer_ref is not None
      and mutable_arraydef.outer_index is not None
      and mutable_arraydef.outer_index in outer_index_outer_ref
    ):
      # if mutable array exists, update it
      mutable_array = outer_index_outer_ref[mutable_arraydef.outer_index]
      if not variablelib.is_mutable_array(mutable_array):
        raise RuntimeError(
          f'Expected a MutableArray type but got {mutable_array}.'
        )
      if type(leaf) is not NoUpdate:
        raise RuntimeError(
          f'Expected a no update for MutableArray but got {leaf}.'
        )
    elif type(leaf) in (NoUpdate, Repeated):
      raise ValueError(
        f"Expected a MutableArrayOutput type but got '{leaf.value}.'"
      )
    elif type(leaf) is MutableArrayOutput:
      mutable_array = jax.experimental.mutable_array(leaf.value)
    elif variablelib.is_mutable_array(leaf):
      mutable_array = leaf
    else:
      # here we allow merging frozen arrays and will not create a new mutable array
      mutable_array = leaf

    index_ref[mutable_arraydef.index] = mutable_array
    return mutable_array

  if type(nodedef) is NodeRef:
    return index_ref[nodedef.index]

  if type(nodedef) is VariableDef:
    variabledef = tp.cast(VariableDef[Variable], nodedef)
    # its a unseen variable, create a new one

    if variabledef.mutable_arraydef is not None:
      if type(variabledef.mutable_arraydef) is NodeRef:
        value = index_ref[variabledef.mutable_arraydef.index]
      else:
        value = next(leaves_iter)
        assert type(variabledef.mutable_arraydef) is MutableArrayDef
        if isinstance(value, Variable):
          value = value.copy() if copy_variables else value
          inner_value = value.raw_value
          mutable_array = get_mutable_array(
            variabledef.mutable_arraydef, inner_value
          )
          if mutable_array is not inner_value:
            value.raw_value = mutable_array
        else:
          # if value is an array or mutable array, we need call get_mutable_array
          # to register it in the index_ref
          value = get_mutable_array(variabledef.mutable_arraydef, value)
    else:
      value = next(leaves_iter)
      if isinstance(value, Variable) and copy_variables:
        value = value.copy()

    # when idxmap is present, check if the Varable exists there
    # and update existing variables if it does
    if (
      outer_index_outer_ref is not None
      and variabledef.outer_index is not None
      and variabledef.outer_index in outer_index_outer_ref
    ):
      # if variable exists, update it
      variable = outer_index_outer_ref[variabledef.outer_index]
      if not isinstance(variable, Variable):
        raise ValueError(f'Expected a Variable type but got {type(variable)}.')
      elif isinstance(value, Variable):
        variable.update_from_state(value)
      else:
        variable.raw_value = value
    else:  # variabledef.index not in index_ref_cache
      # variable reference does not exist outside, create a new one
      if isinstance(value, Variable):
        variable = value
      else:
        variable = variabledef.type.from_metadata(
          value, dict(variabledef.metadata)
        )
    index_ref[variabledef.index] = variable
    return variable  # type: ignore[return-value]

  if type(nodedef) is MutableArrayDef:
    leaf = next(leaves_iter)
    mutable_array = get_mutable_array(nodedef, leaf)
    return mutable_array  # type: ignore[return-value]

  assert type(nodedef) is NodeDef
  if node_impl is None:
    raise RuntimeError(f'Unsupported type: {nodedef.type}, this is a bug.')
  if nodedef.index is not None and nodedef.index in index_ref:
    raise RuntimeError(f'GraphDef index {nodedef.index} already used.')

  def _get_children() -> list[tuple[Key, tp.Any]]:
    children: list[tuple[Key, LeafType | Node]] = []  # type: ignore[invalid-annotation]

    assert type(nodedef) is NodeDef
    for _ in range(nodedef.num_attributes):
      key, value = next(attribute_iter)
      if type(value) is Static:
        children.append((key, value.value))  # type: ignore[attribute-error]
      elif type(value) is MutableArrayAttr:
        mutable_arraydef = next(node_iter)
        assert (
          type(mutable_arraydef) is MutableArrayDef
          or type(mutable_arraydef) is NodeRef
        )
        if type(mutable_arraydef) is NodeRef:
          mutable_array = index_ref[mutable_arraydef.index]
        else:
          assert type(mutable_arraydef) is MutableArrayDef
          leaf = next(leaves_iter)
          mutable_array = get_mutable_array(mutable_arraydef, leaf)
        children.append((key, mutable_array))
      elif type(value) is ArrayAttr:
        array = next(leaves_iter)
        children.append((key, array))
      elif type(value) is NodeRef:
        children.append((key, index_ref[value.index]))  # type: ignore[attribute-error]
      elif type(value) is NodeAttr:
        # if the key is a subgraph we create an empty node
        subgraphdef = next(node_iter)
        if type(subgraphdef) is NodeDef:
          value_node_impl = get_node_impl_for_type(subgraphdef.type)  # type: ignore[attribute-error]
        else:
          value_node_impl = None
        subnode = _graph_unflatten(
          subgraphdef,
          value_node_impl,
          node_iter,
          attribute_iter,
          leaves_iter,
          index_ref,
          outer_index_outer_ref,
          copy_variables,
        )
        children.append((key, subnode))
      else:
        raise RuntimeError(f'Unknown static field: {key!r}')

    return children

  if isinstance(node_impl, GraphNodeImpl):
    # we create an empty node first and add it to the index
    # this avoids infinite recursion when there is a reference cycle
    assert type(nodedef) is NodeDef
    if (
      outer_index_outer_ref is not None
      and nodedef.outer_index is not None
      and nodedef.outer_index in outer_index_outer_ref
    ):
      node = outer_index_outer_ref[nodedef.outer_index]
      if type(node) != nodedef.type:
        raise ValueError(
          f'Expected a node of type {nodedef.type} for index '
          f'{nodedef.index}, but got a node of type {type(node)}.'
        )
      node_impl.clear(node)
    else:
      node = node_impl.create_empty(nodedef.metadata)
    assert nodedef.index is not None
    index_ref[nodedef.index] = node
    node_impl.init(node, _get_children())
  else:
    # if the node type does not support the creation of an empty object it means
    # that it cannot reference itself, so we can create its children first
    node = node_impl.unflatten(_get_children(), nodedef.metadata)

  return node


def graph_pop(
  node: tp.Any,
  filters: tuple[filterlib.Filter, ...],
) -> tuple[GraphState, ...]:
  id_to_index: dict[int, Index] = {}
  path_parts: PathParts = ()
  predicates = tuple(filterlib.to_predicate(filter) for filter in filters)
  flat_states: tuple[dict[PathParts, LeafType], ...] = tuple(
    {} for _ in predicates
  )
  _graph_pop(node, id_to_index, path_parts, flat_states, predicates)
  return tuple(
    statelib.from_flat_state(flat_state) for flat_state in flat_states
  )


def _graph_pop(
  node: tp.Any,
  id_to_index: dict[int, Index],
  path_parts: PathParts,
  flat_states: tuple[dict[PathParts, LeafType], ...],
  predicates: tuple[filterlib.Predicate, ...],
) -> None:
  if not is_node(node):
    raise RuntimeError(f'Unsupported type: {type(node)}, this is a bug.')

  if id(node) in id_to_index:
    return

  id_to_index[id(node)] = len(id_to_index)
  node_impl = get_node_impl(node)
  if node_impl is None:
    raise TypeError(f'Unknown node type: {type(node)}')
  node_dict = node_impl.node_dict(node)

  for name, value in node_dict.items():
    if is_node(value):
      _graph_pop(
        node=value,
        id_to_index=id_to_index,
        path_parts=(*path_parts, name),
        flat_states=flat_states,
        predicates=predicates,
      )
      continue
    elif not is_node_leaf(value):
      continue
    elif id(value) in id_to_index:
      continue

    node_path = (*path_parts, name)
    node_impl = get_node_impl(node)
    if node_impl is None:
      raise TypeError(f'Unknown node type: {type(node)}')

    for state, predicate in zip(flat_states, predicates):
      if predicate(node_path, value):
        if isinstance(node_impl, PytreeNodeImpl):
          raise ValueError(
            f'Cannot pop key {name!r} from node of type {type(node).__name__}'
          )
        id_to_index[id(value)] = len(id_to_index)
        node_impl.pop_key(node, name)
        if isinstance(value, Variable):
          value = value
        state[node_path] = value  # type: ignore[index] # mypy is wrong here?
        break
    else:
      # NOTE: should we raise an error here?
      pass


def _graph_update_dynamic(node: tp.Any, state: tp.Mapping[KeyT, tp.Any]):
  def _update_variable(node: Variable, value):
    if isinstance(value, Variable):
      # updated from Variable
      node.update_from_state(value)
    else:
      # updated from raw value
      if isinstance(value, State) and not value:
        # NOTE: this is a special case when trying to update a Variable from state
        # created when flattening into a NodeRef, which creates an empty State. This
        # can happen when using standalone Variables with `grad`
        pass
      else:
        if is_mutable_array(node.raw_value) and (
          isinstance(value, jax.Array) or is_mutable_array(value)
        ):
          node[...] = value[...]
        else:
          node.raw_value = value

  if isinstance(node, Variable):
    _update_variable(node, state)
    return

  if not is_node(node):
    raise RuntimeError(f'Unsupported type: {type(node)}')

  node_impl = get_node_impl(node)
  if node_impl is None:
    raise TypeError(f'Unknown node type: {type(node)}')
  node_dict = node_impl.node_dict(node)
  for key, value in state.items():
    # case 1: new state is being added
    if key not in node_dict:
      if isinstance(node_impl, PytreeNodeImpl):
        raise ValueError(
          f'Cannot set key {key!r} on immutable node of '
          f'type {type(node).__name__}'
        )
      if isinstance(value, Variable):
        value = value.copy()
      node_impl.set_key(node, key, value)
      continue

    current_value = node_dict[key]

    # case 2: subgraph is being updated
    if is_mutable_array(current_value):
      current_value[...] = value
    elif is_node(current_value):
      if is_node_leaf(value):
        raise ValueError(f'Expected a subgraph for {key!r}, but got: {value!r}')
      _graph_update_dynamic(current_value, value)
    else:
      if isinstance(current_value, jax.Array | np.ndarray):
        if isinstance(node_impl, PytreeNodeImpl):
          raise ValueError(
            f'Cannot set key {key!r} on immutable node of '
            f'type {type(node).__name__}'
          )
        node_impl.set_key(node, key, value)
        continue
      elif not isinstance(current_value, Variable):
        # case 3: state leaf is being updated
        raise ValueError(
          f'Trying to update a non-Variable attribute {key!r} with a Variable: '
          f'{value!r}'
        )
      _update_variable(current_value, value)


# --------------------------------------------------------
# UpdateContext
# --------------------------------------------------------


class StaticCache(tp.NamedTuple):
  graphdef: GraphDef[tp.Any]
  final_graphdef: GraphDef[tp.Any]
  paths: tuple[PathParts, ...]
  variables: list[Variable[tp.Any]]
  new_ref_index: RefMap
  new_index_ref: IndexMap

  @staticmethod
  def create(
    graphdef: GraphDef[tp.Any],
    paths: tuple[PathParts, ...],
    variables: list[Variable[tp.Any]],
    new_ref_index: RefMap,
  ):
    new_index_ref = IndexMap.from_refmap(new_ref_index)
    final_graphdef: GraphDef[tp.Any]
    final_graphdef = graphdef.with_same_outer_index()
    return StaticCache(
      graphdef=graphdef,
      final_graphdef=final_graphdef,
      paths=paths,
      variables=variables,
      new_ref_index=new_ref_index,
      new_index_ref=new_index_ref,
    )


@dataclasses.dataclass
class GraphContext(threading.local):
  update_context_stacks: dict[tp.Hashable, list[UpdateContext]] = (
    dataclasses.field(default_factory=dict)
  )
  ref_index_stack: list[SplitContext] = dataclasses.field(default_factory=list)
  index_ref_stack: list[MergeContext] = dataclasses.field(default_factory=list)
  tmp_static_cache: tp.MutableMapping[tp.Any, StaticCache] | None = None
  caching: bool = False


GRAPH_CONTEXT = GraphContext()


@contextlib.contextmanager
def static_cache(static_cache: tp.MutableMapping[tp.Any, StaticCache]):
  if GRAPH_CONTEXT.caching:
    yield
    return

  GRAPH_CONTEXT.tmp_static_cache = static_cache

  try:
    yield
  finally:
    if GRAPH_CONTEXT.tmp_static_cache is not None:
      raise ValueError(
        'GRAPH_CONTEXT.tmp_static_cache should be None, no context consumed it.'
      )


def _cached_partial(f: tp.Callable[..., tp.Any], *cached_args):
  """Create a partial from a NNX transformed function alog with some cached input arguments
  and reduces the python overhead by caching the traversal of NNX graph nodes. This is useful
  for speed up function that are called repeatedly with the same subset of inputs e.g. a
  ``train_step`` with a ``model`` and ``optimizer``::

    >>> from flax import nnx
    >>> import jax.numpy as jnp
    >>> import optax
    ...
    >>> model = nnx.Linear(2, 3, rngs=nnx.Rngs(0))
    >>> optimizer = nnx.Optimizer(model, optax.adamw(1e-3), wrt=nnx.Param)
    ...
    >>> @nnx.jit
    ... def train_step(model, optimizer, x, y):
    ...   def loss_fn(model):
    ...     return jnp.mean((model(x) - y) ** 2)
    ...
    ...   loss, grads = nnx.value_and_grad(loss_fn)(model)
    ...   optimizer.update(model, grads)
    ...   return loss
    ...
    >>> cached_train_step = nnx.cached_partial(train_step, model, optimizer)
    ...
    >>> for step in range(total_steps:=2):
    ...   x, y = jnp.ones((10, 2)), jnp.ones((10, 3))
    ...   # loss = train_step(model, optimizer, x, y)
    ...   loss = cached_train_step(x, y)
    ...   print(f'Step {step}: loss={loss:.3f}')
    Step 0: loss=2.669
    Step 1: loss=2.660

  Note that ``cached_partial`` will clone all cached graph nodes to gurantee the validity
  of the cache, and these clones will contain references to the same Variable objects
  which guarantees that state is propagated correctly back to the original graph nodes.
  Because of the previous, the final structure of all graph nodes must be the same
  after each call to the cached function, otherswise an error will be raised. Temporary
  mutations are allowed (e.g. the use of ``Module.sow``) as long as they are cleaned up before
  the function returns (e.g. via ``nnx.pop``).

  Args:
    f: A function to cache.
    *cached_args: A subset of the input arguments containing the graph nodes to cache.

  Returns:
    A partial function expecting the remaining arguments to the original function.
  """
  cache: tp.MutableMapping[tp.Any, StaticCache] = PythonRefMap()  # type: ignore
  original_ref_index: RefMap = RefMap()
  index_ref: IndexMap = IndexMap()
  cached_ref_index: RefMap = RefMap()

  def create_static_cache(x):
    # TODO(cgarciae): support Array attribute updates for graph nodes
    if is_graph_node(x) or isinstance(x, Variable):
      graphdef, flat_state = flatten(
        x, with_paths=True, ref_index=original_ref_index
      )
      paths = flat_state.paths
      variables = flat_state.leaves
      # clone but keep the same variable references
      node_cache = unflatten(
        graphdef, flat_state, index_ref=index_ref, copy_variables=False
      )
      cached_new_ref_index = RefMap()
      _fp = fingerprint(
        node_cache,
        ref_index=cached_ref_index,
        new_ref_index=cached_new_ref_index,
      )
      cached_ref_index.update(cached_new_ref_index)
      cache[node_cache] = StaticCache.create(
        graphdef, paths, variables, cached_new_ref_index
      )
      return node_cache
    return x

  cached_args = jax.tree.map(
    create_static_cache,
    cached_args,
    is_leaf=lambda x: is_graph_node(x) or isinstance(x, Variable),
  )

  @functools.wraps(f)
  def cache_args_wrapper(*args, **kwargs):
    with static_cache(cache):
      return f(*cached_args, *args, **kwargs)

  return cache_args_wrapper


if tp.TYPE_CHECKING:
  cached_partial = functools.partial
else:
  cached_partial = _cached_partial


@dataclasses.dataclass
class SplitContext:
  ctxtag: tp.Hashable | None
  ref_index: RefMap
  is_inner: bool | None

  @tp.overload
  def split(self, graph_node: A, /) -> tuple[GraphDef[A], GraphState]: ...  # type: ignore[invalid-annotation]

  @tp.overload
  def split(  # type: ignore[invalid-annotation]
    self, graph_node: A, first: filterlib.Filter, /
  ) -> tuple[GraphDef[A], GraphState]: ...

  @tp.overload
  def split(
    self,
    graph_node: A,
    first: filterlib.Filter,
    second: filterlib.Filter,
    /,
    *filters: filterlib.Filter,
  ) -> tuple[GraphDef[A], GraphState, tpe.Unpack[tuple[GraphState, ...]]]: ...  # type: ignore[not-supported-yet]

  def split(
    self, node: A, *filters: filterlib.Filter
  ) -> tuple[GraphDef[A], tpe.Unpack[tuple[GraphState, ...]]]:  # type: ignore[not-supported-yet]
    ctx = (
      current_update_context(self.ctxtag) if self.ctxtag is not None else None
    )
    inner_ref_outer_index = (
      ctx.inner_ref_outer_index if ctx and ctx.inner_ref_outer_index else None
    )
    graphdef, flat_state = flatten(
      node, ref_index=self.ref_index, ref_outer_index=inner_ref_outer_index
    )
    flat_states = _split_state(flat_state, filters)
    states = _to_nested_state(graphdef, flat_states)

    return graphdef, *states

  @tp.overload
  def flatten(  # type: ignore[invalid-annotation]
    self,
    graph_node: A,
    /,
    *,
    with_paths: tp.Literal[False],
  ) -> tuple[GraphDef[A], list[tp.Any]]: ...

  @tp.overload
  def flatten(  # type: ignore[invalid-annotation]
    self,
    graph_node: A,
    /,
  ) -> tuple[GraphDef[A], FlatState[tp.Any]]: ...

  @tp.overload
  def flatten(  # type: ignore[invalid-annotation]
    self,
    graph_node: A,
    first: filterlib.Filter,
    /,
  ) -> tuple[GraphDef[A], FlatState[tp.Any]]: ...

  @tp.overload
  def flatten(  # type: ignore[invalid-annotation]
    self,
    graph_node: A,
    first: filterlib.Filter,
    second: filterlib.Filter,
    /,
    *filters: filterlib.Filter,
  ) -> tuple[
    GraphDef[A],
    FlatState[tp.Any],
    tpe.Unpack[tuple[FlatState[tp.Any], ...]],
  ]: ...

  def flatten(  # type: ignore[invalid-annotation]
    self,
    node: A,
    *filters: filterlib.Filter,
    with_paths: bool = True,
  ) -> tuple[
    GraphDef[A],
    FlatState[tp.Any] | list[tp.Any],
    tpe.Unpack[tuple[FlatState[tp.Any], ...]],
  ]:
    if not with_paths and filters:
      raise ValueError('Cannot use filters with with_paths=False')

    ctx = (
      current_update_context(self.ctxtag) if self.ctxtag is not None else None
    )
    static_cache = (
      ctx.static_cache if ctx is not None and self.is_inner is False else None
    )
    ref_outer_index = (
      ctx.inner_ref_outer_index if ctx and ctx.inner_ref_outer_index else None
    )
    flat_state: FlatState[tp.Any] | list[tp.Any]
    leaves: list[tp.Any]
    if node in self.ref_index:
      # node is already in the ref_index, call flatten which will return a NodeRef
      graphdef, flat_state = flatten(
        node,
        ref_index=self.ref_index,
        ref_outer_index=ref_outer_index,
        with_paths=with_paths,
      )
      if with_paths:
        assert isinstance(flat_state, FlatState)
        paths = flat_state.paths
        leaves = flat_state.leaves
      else:
        assert isinstance(flat_state, list)
        paths = None
        leaves = flat_state
    elif static_cache is not None and node in static_cache:
      node_static_cache = static_cache[node]
      graphdef = node_static_cache.graphdef
      # add the new references to the ref_index
      self.ref_index.update(node_static_cache.new_ref_index)

      if with_paths:
        paths = node_static_cache.paths
        leaves = node_static_cache.variables
      else:
        paths = None
        leaves = [
          variable.raw_value for variable in node_static_cache.variables
        ]
    else:
      graphdef, flat_state = flatten(
        node,
        ref_index=self.ref_index,
        ref_outer_index=ref_outer_index,
        with_paths=with_paths,
      )
      if with_paths:
        assert isinstance(flat_state, FlatState)
        paths = flat_state.paths
        leaves = flat_state.leaves
      else:
        assert isinstance(flat_state, list)
        paths = None
        leaves = flat_state

    if with_paths:
      assert paths is not None
      flat_state = FlatState.from_sorted_keys_values(paths, leaves)
      flat_states = _split_state(flat_state, filters)
      return graphdef, *flat_states  # type: ignore[bad-return-type]
    else:
      return graphdef, leaves


@contextlib.contextmanager
def split_context(ctxtag: tp.Hashable | None = None):
  ctx = current_update_context(ctxtag) if ctxtag is not None else None
  is_inner = ctx.outer_ref_outer_index is not None if ctx is not None else None
  GRAPH_CONTEXT.ref_index_stack.append(SplitContext(ctxtag, RefMap(), is_inner))

  try:
    yield GRAPH_CONTEXT.ref_index_stack[-1]
  finally:
    flatten_ctx = GRAPH_CONTEXT.ref_index_stack.pop()
    if ctxtag is not None:
      ctx = current_update_context(ctxtag)
      ctx.flatten_end(flatten_ctx.ref_index)
    del flatten_ctx.ref_index
    del flatten_ctx.ctxtag


@dataclasses.dataclass
class MergeContext:
  ctxtag: tp.Hashable | None
  index_ref: IndexMap
  is_inner: bool | None

  def merge(  # type: ignore[invalid-annotation]
    self,
    graphdef: GraphDef[A],
    state: GraphState,
    /,
    *states: GraphState,
  ) -> A:
    ctx = (
      current_update_context(self.ctxtag) if self.ctxtag is not None else None
    )
    outer_index_outer_ref = (
      ctx.outer_index_outer_ref if ctx and ctx.outer_index_outer_ref else None
    )

    _state = _merge_to_flat_state((state, *states))
    node = unflatten(
      graphdef,
      _state,
      index_ref=self.index_ref,
      outer_index_outer_ref=outer_index_outer_ref,
    )
    return node

  def unflatten(  # type: ignore[invalid-annotation]
    self,
    graphdef: GraphDef[A],
    flat_state: GraphFlatState | list[tp.Any],
    /,
    *flat_states: GraphFlatState,
  ) -> A:
    ctx = (
      current_update_context(self.ctxtag) if self.ctxtag is not None else None
    )
    static_cache = (
      ctx.static_cache if ctx is not None and self.is_inner is False else None
    )
    state: FlatState[tp.Any] | list[tp.Any]
    if type(flat_state) is list:
      if flat_states:
        raise ValueError(
          'Cannot use multiple flat_states when flat_state is a list, '
          f'got flat_state: {flat_state!r}, flat_states: {flat_states!r}'
        )
      state = flat_state
    else:
      state = FlatState.merge(flat_state, *flat_states)

    if type(graphdef.nodes[0]) is NodeRef:
      node = unflatten(
        graphdef,
        state,
        index_ref=self.index_ref,
      )

    elif static_cache is not None:
      assert isinstance(graphdef.nodes[0], NodeDef)
      assert ctx is not None
      if (outer_index := graphdef.nodes[0].outer_index) is not None:
        outer_index_outer_ref = ctx.outer_index_outer_ref
        assert outer_index_outer_ref is not None
        node = outer_index_outer_ref[outer_index]

        if node in static_cache:
          static_cache_node = static_cache[node]
          if static_cache_node.final_graphdef != graphdef:
            raise ValueError(
              'The graph structure of a node added to cached_partial was mutated inside the transformation, '
              f'this is not allowed.\nNode: {node}\nOuput graphdef: {graphdef}\nExpected graphdef: {static_cache_node.final_graphdef}'
            )
          if type(state) is list:
            leaves = state
          elif type(state) is FlatState:
            leaves = state.leaves
          else:
            raise ValueError(f'Unsupported state type: {type(state)}')

          if len(leaves) != len(static_cache_node.variables):
            raise ValueError(
              f'Incorrect number of leaves: expected {len(static_cache_node.variables)} '
              f'leaves in the state, got {len(leaves)}'
            )
          for variable, leaf in zip(static_cache_node.variables, leaves):
            if isinstance(leaf, Variable):
              variable.update_from_state(leaf)
            else:
              variable.raw_value = leaf
          self.index_ref.update(static_cache_node.new_index_ref)
        else:
          # uncached node, create it
          node = unflatten(
            graphdef,
            state,
            index_ref=self.index_ref,
            outer_index_outer_ref=outer_index_outer_ref,
          )
      else:  # graphdef.outer_index is None
        # its a new node, create it
        node = unflatten(
          graphdef,
          state,
          index_ref=self.index_ref,
        )
    else:
      outer_index_outer_ref = (
        ctx.outer_index_outer_ref if ctx and ctx.outer_index_outer_ref else None
      )
      node = unflatten(
        graphdef,
        state,
        index_ref=self.index_ref,
        outer_index_outer_ref=outer_index_outer_ref,
      )
    return node


@tp.overload
@contextlib.contextmanager
def merge_context() -> tp.Generator[MergeContext, None, None]: ...  # type: ignore[bad-return-type]
@tp.overload
@contextlib.contextmanager
def merge_context(
  ctxtag: tp.Hashable | None, inner: bool | None
) -> tp.Generator[MergeContext, None, None]: ...  # type: ignore[bad-return-type]
@contextlib.contextmanager
def merge_context(ctxtag: tp.Hashable | None = None, inner: bool | None = None):
  GRAPH_CONTEXT.index_ref_stack.append(MergeContext(ctxtag, IndexMap(), inner))

  try:
    yield GRAPH_CONTEXT.index_ref_stack[-1]
  finally:
    unflatten_ctx = GRAPH_CONTEXT.index_ref_stack.pop()
    index_ref = unflatten_ctx.index_ref
    if ctxtag is not None:
      if inner is None:
        raise ValueError('inner_merge must be specified when using ctxtag')
      ctx = current_update_context(ctxtag)
      ctx.unflatten_end(index_ref, inner)
    del unflatten_ctx.index_ref
    del unflatten_ctx.ctxtag


@jax.tree_util.register_static
@dataclasses.dataclass
class UpdateContext:
  """A context manager for handling complex state updates."""

  tag: tp.Hashable
  outer_ref_outer_index: RefMap | None
  outer_index_inner_ref: IndexMap | None
  # reverse caches
  outer_index_outer_ref: IndexMap | None
  inner_ref_outer_index: RefMap | None
  static_cache: tp.MutableMapping[tp.Any, StaticCache] | None

  # define hash and eq to make this an opaque object
  def __hash__(self):
    return 0

  def __eq__(self, other):
    return isinstance(other, UpdateContext)

  def flatten_end(self, ref_index: RefMap):
    if self.outer_ref_outer_index is None:
      # outer split (1), store the references
      self.outer_ref_outer_index = ref_index
      self.outer_index_outer_ref = IndexMap.from_refmap(
        self.outer_ref_outer_index
      )
    else:
      # inner split (3), clear index_ref
      self.outer_index_inner_ref = None
      self.inner_ref_outer_index = None

  def unflatten_end(self, index_ref: IndexMap, inner_merge: bool):
    if inner_merge:
      # inner merge (2)
      self.outer_index_inner_ref = index_ref
      self.inner_ref_outer_index = RefMap.from_indexmap(index_ref)


@dataclasses.dataclass
class UpdateContextManager:
  tag: tp.Hashable

  def __enter__(self):
    if GRAPH_CONTEXT.tmp_static_cache is not None:
      # take current static cache
      static_cache = GRAPH_CONTEXT.tmp_static_cache
      GRAPH_CONTEXT.tmp_static_cache = None
    else:
      static_cache = None
    ctx = UpdateContext(
      tag=self.tag,
      outer_ref_outer_index=None,
      outer_index_inner_ref=None,
      outer_index_outer_ref=None,
      inner_ref_outer_index=None,
      static_cache=static_cache,
    )
    if self.tag not in GRAPH_CONTEXT.update_context_stacks:
      GRAPH_CONTEXT.update_context_stacks[self.tag] = [ctx]
    else:
      GRAPH_CONTEXT.update_context_stacks[self.tag].append(ctx)
    return ctx

  def __exit__(self, *args):
    if self.tag not in GRAPH_CONTEXT.update_context_stacks:
      raise RuntimeError(
        f'No update context found for tag {self.tag!r}, this is a bug.'
      )
    stack = GRAPH_CONTEXT.update_context_stacks[self.tag]

    ctx = stack.pop()
    # clear references
    del ctx.outer_ref_outer_index
    del ctx.outer_index_inner_ref
    del ctx.outer_index_outer_ref
    del ctx.inner_ref_outer_index

    if not stack:
      del GRAPH_CONTEXT.update_context_stacks[self.tag]

  def __call__(self, f: F) -> F:
    @functools.wraps(f)
    def update_context_manager_wrapper(*args, **kwargs):
      with self:
        return f(*args, **kwargs)

    return update_context_manager_wrapper  # type: ignore


def update_context(tag: tp.Hashable):
  """Creates an :class:`UpdateContext` context manager which can be used to handle
  more complex state updates beyond what ``nnx.update`` can handle, including
  updates to static properties and graph structure.

  UpdateContext exposes a ``split`` and ``merge`` API with the same
  signature as ``nnx.split`` / ``nnx.merge`` but performs some bookkeeping
  to have the necessary information in order to perfectly update the input
  objects based on the changes made inside the transform. The UpdateContext
  must call split and merge a total of 4 times, the first
  and last calls happen outside the transform and the second and third calls
  happen inside the transform as shown in the diagram below::


                          idxmap
    (2) merge ─────────────────────────────► split (3)
          ▲                                    │
          │               inside               │
          │. . . . . . . . . . . . . . . . . . │ index_mapping
          │               outside              │
          │                                    ▼
    (1) split──────────────────────────────► merge (4)
                          refmap


  The first call to split ``(1)`` creates a ``refmap`` which keeps track of the
  outer references, and the first call to merge ``(2)`` creates an ``idxmap`` which
  keeps track of the inner references. The second call to split ``(3)`` combines
  the refmap and idxmap to produce the ``index_mapping`` which indicates
  how the outer references map to the inner references. Finally, the last call to
  merge ``(4)`` uses the index_mapping and the refmap to reconstruct the
  output of the transform while reusing/updating the inner references. To avoid
  memory leaks, the idxmap is cleared after ``(3)`` and the refmap is
  cleared after ``(4)``, and both are cleared after the context manager exits.

  Here is a simple example showing the use of ``update_context``::

    >>> from flax import nnx
    ...
    >>> class Foo(nnx.Module): pass
    ...
    >>> m1 = Foo()
    >>> with nnx.update_context('example'):
    ...   with nnx.split_context('example') as ctx:
    ...     graphdef, state = ctx.split(m1)
    ...   @jax.jit
    ...   def f(graphdef, state):
    ...     with nnx.merge_context('example', inner=True) as ctx:
    ...       m2 = ctx.merge(graphdef, state)
    ...     m2.a = 1
    ...     m2.ref = m2  # create a reference cycle
    ...     with nnx.split_context('example') as ctx:
    ...       return ctx.split(m2)
    ...   graphdef_out, state_out = f(graphdef, state)
    ...   with nnx.merge_context('example', inner=False) as ctx:
    ...     m3 = ctx.merge(graphdef_out, state_out)
    ...
    >>> assert m1 is m3
    >>> assert m1.a == 1
    >>> assert m1.ref is m1

  Note that ``update_context`` takes in a ``tag`` argument which is used
  primarily as a safety mechanism reduce the risk of accidentally using the
  wrong UpdateContext when using :func:`current_update_context` to access the
  current active context. ``update_context`` can also be used as a
  decorator that creates/activates an UpdateContext context for the
  duration of the function::

    >>> from flax import nnx
    ...
    >>> class Foo(nnx.Module): pass
    ...
    >>> m1 = Foo()
    >>> @jax.jit
    ... def f(graphdef, state):
    ...   with nnx.merge_context('example', inner=True) as ctx:
    ...     m2 = ctx.merge(graphdef, state)
    ...   m2.a = 1     # insert static attribute
    ...   m2.ref = m2  # create a reference cycle
    ...   with nnx.split_context('example') as ctx:
    ...     return ctx.split(m2)
    ...
    >>> @nnx.update_context('example')
    ... def g(m1):
    ...   with nnx.split_context('example') as ctx:
    ...     graphdef, state = ctx.split(m1)
    ...   graphdef_out, state_out = f(graphdef, state)
    ...   with nnx.merge_context('example', inner=False) as ctx:
    ...     return ctx.merge(graphdef_out, state_out)
    ...
    >>> m3 = g(m1)
    >>> assert m1 is m3
    >>> assert m1.a == 1
    >>> assert m1.ref is m1

  The context can be accessed using :func:`current_update_context`.

  Args:
    tag: A string tag to identify the context.
  """
  return UpdateContextManager(tag=tag)


def current_update_context(tag: tp.Hashable) -> UpdateContext:
  """Returns the current active :class:`UpdateContext` for the given tag."""
  if tag not in GRAPH_CONTEXT.update_context_stacks:
    raise ValueError(f'No update context found for tag {tag!r}.')
  return GRAPH_CONTEXT.update_context_stacks[tag][-1]


# --------------------------------------------------------
# Functional API
# --------------------------------------------------------


def _split_state(
  state: FlatState[tp.Any],
  filters: tuple[filterlib.Filter, ...],
) -> tuple[FlatState[tp.Any], tpe.Unpack[tuple[FlatState[tp.Any], ...]]]:
  if not filters:
    return (state,)  # type: ignore[bad-return-type]
  states = state.split(*filters)
  if not isinstance(states, tuple):
    return (states,)  # type: ignore[bad-return-type]
  assert len(states) > 0
  return states  # type: ignore[return-value]


@tp.overload
def split(  # type: ignore[invalid-annotation]
  graph_node: A, /
) -> tuple[GraphDef[A], GraphState]: ...
@tp.overload
def split(  # type: ignore[invalid-annotation]
  graph_node: A, first: filterlib.Filter, /
) -> tuple[GraphDef[A], GraphState]: ...
@tp.overload
def split(  # type: ignore[invalid-annotation]
  graph_node: A,
  first: filterlib.Filter,
  second: filterlib.Filter,
  /,
  *filters: filterlib.Filter,
) -> tuple[
  GraphDef[A],
  GraphState,
  tpe.Unpack[tuple[GraphState, ...]],
]: ...
def split(  # type: ignore[invalid-annotation]
  node: A, *filters: filterlib.Filter
) -> tuple[
  GraphDef[A],
  GraphState,
  tpe.Unpack[tuple[GraphState, ...]],
]:
  """Split a graph node into a :class:`GraphDef` and one or more :class:`State`s. State is
  a ``Mapping`` from strings or integers to ``Variables``, Arrays or nested States. GraphDef
  contains all the static information needed to reconstruct a ``Module`` graph, it is analogous
  to JAX’s ``PyTreeDef``. :func:`split` is used in conjunction with :func:`merge` to switch
  seamlessly between stateful and stateless representations of the graph.

  Example usage::

    >>> from flax import nnx
    >>> import jax, jax.numpy as jnp
    ...
    >>> class Foo(nnx.Module):
    ...   def __init__(self, rngs):
    ...     self.batch_norm = nnx.BatchNorm(2, rngs=rngs)
    ...     self.linear = nnx.Linear(2, 3, rngs=rngs)
    ...
    >>> node = Foo(nnx.Rngs(0))
    >>> graphdef, params, batch_stats = nnx.split(node, nnx.Param, nnx.BatchStat)
    ...
    >>> jax.tree.map(jnp.shape, params)
    State({
      'batch_norm': {
        'bias': Param(
          value=(2,)
        ),
        'scale': Param(
          value=(2,)
        )
      },
      'linear': {
        'bias': Param(
          value=(3,)
        ),
        'kernel': Param(
          value=(2, 3)
        )
      }
    })
    >>> jax.tree.map(jnp.shape, batch_stats)
    State({
      'batch_norm': {
        'mean': BatchStat(
          value=(2,)
        ),
        'var': BatchStat(
          value=(2,)
        )
      }
    })

  :func:`split` and :func:`merge` are primarily used to interact directly with JAX
  transformations, see
  `Functional API <https://flax.readthedocs.io/en/latest/nnx/nnx_basics.html#the-functional-api>`__
  for more information.

  Arguments:
    node: graph node to split.
    *filters: some optional filters to group the state into mutually exclusive substates.
  Returns:
    ``GraphDef`` and one or more ``States`` equal to the number of filters passed. If no
    filters are passed, a single ``State`` is returned.
  """
  graphdef, flat_state = flatten(node)
  flat_states = _split_state(flat_state, filters)
  states = _to_nested_state(graphdef, flat_states)
  return graphdef, *states  # type: ignore[return-value]


def _to_nested_state(
  graphdef: GraphDef[A], flat_states: tp.Iterable[tp.Any]
) -> tuple[tp.Any, ...]:
  if not graphdef.nodes or type(graphdef.nodes[0]) in (
    VariableDef,
    MutableArrayDef,
  ):
    states = tuple(
      flat_state[0][1] if flat_state else State({})
      for flat_state in flat_states
    )
  else:
    states = tuple(
      statelib.from_flat_state(flat_state) for flat_state in flat_states
    )
  return states


def _merge_to_flat_state(states: tp.Iterable[tp.Any]):
  flat_state: list[tuple[PathParts, tp.Any]] = []

  for state in states:
    if isinstance(state, dict | State):
      flat_state.extend(traversals.flatten_to_sequence(state))
    elif isinstance(state, FlatState):
      flat_state.extend(state)
    else:
      flat_state.append(((), state))

  flat_state.sort()
  return [value for _, value in flat_state]


def merge(  # type: ignore[invalid-annotation]
  graphdef: GraphDef[A],
  state: tp.Any,
  /,
  *states: tp.Any,
) -> A:
  """The inverse of :func:`flax.nnx.split`.

  ``nnx.merge`` takes a :class:`flax.nnx.GraphDef` and one or more :class:`flax.nnx.State`'s
  and creates a new node with the same structure as the original node.

  Recall: :func:`flax.nnx.split` is used to represent a :class:`flax.nnx.Module`
  by: 1) a static ``nnx.GraphDef`` that captures its Pythonic static information;
  and 2) one or more :class:`flax.nnx.Variable` ``nnx.State``'(s) that capture
  its ``jax.Array``'s in the form of JAX pytrees.

  ``nnx.merge`` is used in conjunction with ``nnx.split`` to switch seamlessly
  between stateful and stateless representations of the graph.

  Example usage::

    >>> from flax import nnx
    >>> import jax, jax.numpy as jnp
    ...
    >>> class Foo(nnx.Module):
    ...   def __init__(self, rngs):
    ...     self.batch_norm = nnx.BatchNorm(2, rngs=rngs)
    ...     self.linear = nnx.Linear(2, 3, rngs=rngs)
    ...
    >>> node = Foo(nnx.Rngs(0))
    >>> graphdef, params, batch_stats = nnx.split(node, nnx.Param, nnx.BatchStat)
    ...
    >>> new_node = nnx.merge(graphdef, params, batch_stats)
    >>> assert isinstance(new_node, Foo)
    >>> assert isinstance(new_node.batch_norm, nnx.BatchNorm)
    >>> assert isinstance(new_node.linear, nnx.Linear)

  ``nnx.split`` and ``nnx.merge`` are primarily used to interact directly with JAX
  transformations (refer to
  `Functional API <https://flax.readthedocs.io/en/latest/nnx_basics.html#the-flax-functional-api>`__
  for more information.

  Args:
    graphdef: A :class:`flax.nnx.GraphDef` object.
    state: A :class:`flax.nnx.State` object.
    *states: Additional :class:`flax.nnx.State` objects.
  Returns:
    The merged :class:`flax.nnx.Module`.
  """
  if isinstance(state, list):
    if len(states) != 0:
      raise ValueError(f'Only one state can be passed as a list.')
    _state = state
  else:
    _state = _merge_to_flat_state((state, *states))
  node = unflatten(graphdef, _state)
  return node


def update(node, state: tp.Any, /, *states: tp.Any) -> None:
  """Update the given graph node with a new state(s) in-place.

  Example usage::

    >>> from flax import nnx
    >>> import jax, jax.numpy as jnp

    >>> x = jnp.ones((1, 2))
    >>> y = jnp.ones((1, 3))
    >>> model = nnx.Linear(2, 3, rngs=nnx.Rngs(0))

    >>> def loss_fn(model, x, y):
    ...   return jnp.mean((y - model(x))**2)
    >>> prev_loss = loss_fn(model, x, y)

    >>> grads = nnx.grad(loss_fn)(model, x, y)
    >>> new_state = jax.tree.map(lambda p, g: p - 0.1*g, nnx.state(model), grads)
    >>> nnx.update(model, new_state)
    >>> assert loss_fn(model, x, y) < prev_loss

  Args:
    node: A graph node to update.
    state: A :class:`State` object.
    *states: Additional :class:`State` objects.
  """
  if states:
    if isinstance(node, Variable):
      non_empty_states = [
        _state
        for _state in (state, *states)
        if not isinstance(_state, tp.Mapping) or _state
      ]
      if len(non_empty_states) != 1:
        all_states = (state, *states)
        raise ValueError(
          f'Expected exactly one non-empty state, got: {all_states!r}'
        )
      state = non_empty_states[0]
    else:
      state = statelib.merge_state(state, *states)
  _graph_update_dynamic(node, state)


def _variables_generator(node) -> tp.Iterable[tuple[PathParts, Variable]]:
  for path, value in iter_graph(node):
    if isinstance(value, Variable):
      yield path, value


@tp.overload
def state(node, /) -> GraphState: ...
@tp.overload
def state(node, first: filterlib.Filter, /) -> GraphState: ...
@tp.overload
def state(
  node,
  first: filterlib.Filter,
  second: filterlib.Filter,
  /,
  *filters: filterlib.Filter,
) -> tuple[GraphState, ...]: ...
def state(
  node,
  *filters: filterlib.Filter,
) -> tp.Union[GraphState, tuple[GraphState, ...]]:
  """Similar to :func:`split` but only returns the :class:`State`'s indicated by the filters.

  Example usage::

    >>> from flax import nnx

    >>> class Model(nnx.Module):
    ...   def __init__(self, rngs):
    ...     self.batch_norm = nnx.BatchNorm(2, rngs=rngs)
    ...     self.linear = nnx.Linear(2, 3, rngs=rngs)
    ...   def __call__(self, x):
    ...     return self.linear(self.batch_norm(x))

    >>> model = Model(rngs=nnx.Rngs(0))
    >>> # get the learnable parameters from the batch norm and linear layer
    >>> params = nnx.state(model, nnx.Param)
    >>> # get the batch statistics from the batch norm layer
    >>> batch_stats = nnx.state(model, nnx.BatchStat)
    >>> # get them separately
    >>> params, batch_stats = nnx.state(model, nnx.Param, nnx.BatchStat)
    >>> # get them together
    >>> state = nnx.state(model)

  Args:
    node: A graph node object.
    *filters: One or more :class:`Variable` objects to filter by.
  Returns:
    One or more :class:`State` mappings.
  """
  _, flat_state = flatten(node)
  state = flat_state.to_nested_state()

  states: GraphState | tuple[GraphState, ...]
  if len(filters) == 0:
    states = state  # type: ignore[assignment]
  elif len(filters) == 1:
    states = statelib.filter_state(state, filters[0])
  else:
    states = statelib.filter_state(state, filters[0], filters[1], *filters[2:])

  return states


variables = state


def graphdef(node: tp.Any, /) -> GraphDef[tp.Any]:
  """Get the :class:`GraphDef` of the given graph node.

  Example usage::

    >>> from flax import nnx

    >>> model = nnx.Linear(2, 3, rngs=nnx.Rngs(0))
    >>> graphdef, _ = nnx.split(model)
    >>> assert graphdef == nnx.graphdef(model)

  Args:
    node: A graph node object.
  Returns:
    The :class:`GraphDef` of the :class:`Module` object.
  """
  graphdef, _ = flatten(node)
  return graphdef


@tp.overload
def pop(
  node,
  filter: filterlib.Filter,
  /,
) -> GraphState: ...


@tp.overload
def pop(
  node,
  filter: filterlib.Filter,
  filter2: filterlib.Filter,
  /,
  *filters: filterlib.Filter,
) -> tuple[GraphState, ...]: ...


def pop(
  node, *filters: filterlib.Filter
) -> tp.Union[GraphState, tuple[GraphState, ...]]:
  """Pop one or more :class:`Variable` types from the graph node.

  Example usage::

    >>> from flax import nnx
    >>> import jax.numpy as jnp

    >>> class Model(nnx.Module):
    ...   def __init__(self, rngs):
    ...     self.linear1 = nnx.Linear(2, 3, rngs=rngs)
    ...     self.linear2 = nnx.Linear(3, 4, rngs=rngs)
    ...   def __call__(self, x):
    ...     x = self.linear1(x)
    ...     self.sow(nnx.Intermediate, 'i', x)
    ...     x = self.linear2(x)
    ...     return x

    >>> x = jnp.ones((1, 2))
    >>> model = Model(rngs=nnx.Rngs(0))
    >>> assert not hasattr(model, 'i')
    >>> y = model(x)
    >>> assert hasattr(model, 'i')

    >>> intermediates = nnx.pop(model, nnx.Intermediate)
    >>> assert intermediates['i'].value[0].shape == (1, 3)
    >>> assert not hasattr(model, 'i')

  Args:
    node: A graph node object.
    *filters: One or more :class:`Variable` objects to filter by.
  Returns:
    The popped :class:`State` containing the :class:`Variable`
    objects that were filtered for.
  """
  if len(filters) == 0:
    raise ValueError('Expected at least one filter')

  id_to_index: dict[int, Index] = {}
  path_parts: PathParts = ()
  predicates = tuple(filterlib.to_predicate(filter) for filter in filters)
  flat_states: tuple[dict[PathParts, LeafType], ...] = tuple(
    {} for _ in predicates
  )
  _graph_pop(
    node=node,
    id_to_index=id_to_index,
    path_parts=path_parts,
    flat_states=flat_states,
    predicates=predicates,
  )
  states = tuple(
    statelib.from_flat_state(flat_state) for flat_state in flat_states
  )

  if len(states) == 1:
    return states[0]
  else:
    return states


def clone(node: Node) -> Node:
  """Create a deep copy of the given graph node.

  Example usage::

    >>> from flax import nnx

    >>> model = nnx.Linear(2, 3, rngs=nnx.Rngs(0))
    >>> cloned_model = nnx.clone(model)
    >>> model.bias.value += 1
    >>> assert (model.bias.value != cloned_model.bias.value).all()

  Args:
    node: A graph node object.
  Returns:
    A deep copy of the :class:`Module` object.
  """
  graphdef, state = split(node)
  return merge(graphdef, state)


def find_duplicates(
  tree, duplicate_fn: tp.Callable[[tuple[Key, ...], tp.Any], bool] | None = None
) -> tuple[str, str] | None:
  mutable_arrays: dict[int, str] = {}
  paths_leaves = jax.tree.leaves_with_path(
    tree, is_leaf=lambda x: isinstance(x, Variable)
  )
  for path, x in paths_leaves:
    nnx_path = jax_to_nnx_path(path)
    if duplicate_fn is None or duplicate_fn(nnx_path, x):
      m_array_id = id(x)
      if m_array_id in mutable_arrays:
        current_path_str = jax.tree_util.keystr(path)
        previous_path_str = mutable_arrays[m_array_id]
        return current_path_str, previous_path_str
      mutable_arrays[m_array_id] = jax.tree_util.keystr(path)

  return None


def _mutable_like(path, x):
  return (
    isinstance(x, Variable) and x.mutable
  ) or variablelib.is_mutable_array(x)


def freeze(
  node: A,
  /,
  *,
  only: filterlib.Filter = _mutable_like,
  allow_duplicates: bool = False,
) -> A:
  """Converts a structure of mutable arrays to regular arrays.

  Example::
    >>> from flax import nnx
    >>> import jax
    >>> import jax.numpy as jnp
    ...
    >>> node = [nnx.mutable_array(jnp.array(1.0)), jnp.array(2.0)]
    >>> assert nnx.is_mutable_array(node[0])
    ...
    >>> frozen_node = nnx.freeze(node)
    >>> assert isinstance(frozen_node[0], jax.Array)

  If the structure contains duplicate mutable arrays, a ValueError is raised::

    >>> shared_array = nnx.mutable_array(jnp.array(1.0))
    >>> node = [shared_array, shared_array]
    >>> try:
    ...   nnx.freeze(node)
    ... except ValueError as e:
    ...   print(e)
    Found duplicate at path '[1]' and '[0]'.

  ``only`` is a `Filter <https://flax.readthedocs.io/en/latest/guides/filters_guide.html>`__
  that can be used to specify which mutable arrays to freeze::

    >>> node = [nnx.mutable_array(jnp.array(1.0)), nnx.mutable_array(jnp.array(2.0))]
    >>> frozen_node = nnx.freeze(node, only=lambda path, x: path[0] == 0)
    ...
    >>> assert isinstance(frozen_node[0], jax.Array)
    >>> assert isinstance(frozen_node[1], nnx.MutableArray)

  Args:
    node: A structure potentially containing mutable arrays.
    only: A Filter to specify which mutable arrays to freeze.
  Returns:
    A structure with the frozen arrays.
  """
  duplicate_fn = filterlib.to_predicate(only)
  if (
    not allow_duplicates
    and (duplicate := find_duplicates(node, duplicate_fn=duplicate_fn))
    is not None
  ):
    current_path_str, previous_path_str = duplicate
    raise ValueError(
      f"Found duplicate at path '{current_path_str}' and '{previous_path_str}'."
    )
  graphdef, mutable_state, rest = split(node, only, ...)  # type: ignore[misc]
  frozen_state = jax.tree.map(lambda x: x[...], mutable_state)
  node = merge(graphdef, frozen_state, rest)
  return node


def _array_like(path, x):
  return (isinstance(x, Variable) and not x.mutable) or isinstance(x, jax.Array)


def mutable(node: A, /, only: filterlib.Filter = _array_like) -> A:
  """Converts a structure of arrays to mutable arrays.

  Example::

    >>> from flax import nnx
    >>> import jax
    >>> import jax.numpy as jnp
    ...
    >>> node = [jnp.array(1.0), nnx.mutable_array(jnp.array(2.0))]
    >>> mutable_node = nnx.mutable(node)
    >>> assert nnx.is_mutable_array(mutable_node[0])
    >>> assert nnx.is_mutable_array(mutable_node[1])

  If the structure contains duplicate arrays a ValueError is raised::

    >>> shared_array = jnp.array(1.0)
    >>> node = [shared_array, shared_array]
    >>> try:
    ...   nnx.mutable(node)
    ... except ValueError as e:
    ...   print(e)
    Found duplicate at path '[1]' and '[0]'.

  ``only`` is a `Filter <https://flax.readthedocs.io/en/latest/guides/filters_guide.html>`__
  that can be used to specify which arrays to convert to mutable arrays.

    >>> node = [jnp.array(1.0), jnp.array(2.0)]
    >>> mutable_node = nnx.mutable(node, only=lambda path, x: path[0] == 0)
    ...
    >>> assert isinstance(mutable_node[0], nnx.MutableArray)
    >>> assert isinstance(mutable_node[1], jax.Array)

  Args:
    node: A structure potentially containing arrays.
    only: A Filter to specify which arrays to convert to mutable arrays.
  Returns:
    A structure with the mutable arrays.
  """
  duplicate_fn = filterlib.to_predicate(only)
  if (
    duplicate := find_duplicates(node, duplicate_fn=duplicate_fn)
  ) is not None:
    current_path_str, previous_path_str = duplicate
    raise ValueError(
      f"Found duplicate at path '{current_path_str}' and '{previous_path_str}'."
    )
  graphdef, frozen_state, rest = split(node, only, ...)  # type: ignore[misc]
  mutable_state = jax.tree.map(variablelib.mutable_array, frozen_state)
  node = merge(graphdef, mutable_state, rest)
  return node


def pure(tree: A) -> A:
  """Returns a new tree with all ``Variable`` objects replaced with inner values.

  This can be used to remove Variable metadata when its is not needed for tasks like
  serialization or exporting.

  Example::

    >>> from flax import nnx
    >>> import jax
    >>> import jax.numpy as jnp
    ...
    >>> model = nnx.Linear(2, 3, rngs=nnx.Rngs(0))
    >>> graphdef, state = nnx.split(model)
    >>> jax.tree.map(jnp.shape, state)
    State({
      'bias': Param(
        value=(3,)
      ),
      'kernel': Param(
        value=(2, 3)
      )
    })
    >>> pure_state = nnx.pure(state)
    >>> jax.tree.map(jnp.shape, pure_state)
    State({
      'bias': (3,),
      'kernel': (2, 3)
    })

  Args:
    tree: A pytree potentially containing ``Variable`` objects.
  Returns:
    A new pytree with all ``Variable`` objects replaced with their
    inner values.
  """

  def _pure_fn(x):
    if isinstance(x, Variable):
      return x.raw_value
    return x

  return jax.tree.map(
    _pure_fn,
    tree,
    is_leaf=lambda x: isinstance(x, Variable),
  )


def call(
  graphdef_state: tuple[GraphDef[A], GraphState], /
) -> ApplyCaller[tuple[GraphDef[A], GraphState]]:
  """Calls a method underlying graph node defined by a (GraphDef, State) pair.

  ``call`` takes a ``(GraphDef, State)`` pair and creates a proxy object that can be
  used to call methods on the underlying graph node. When a method is called, the
  output is returned along with a new (GraphDef, State) pair that represents the
  updated state of the graph node. ``call`` is equivalent to :func:`merge` > ``method``
  > :func:`split`` but is more convenient to use in pure JAX functions.

  Example::

    >>> from flax import nnx
    >>> import jax
    >>> import jax.numpy as jnp
    ...
    >>> class StatefulLinear(nnx.Module):
    ...   def __init__(self, din, dout, rngs):
    ...     self.w = nnx.Param(jax.random.uniform(rngs(), (din, dout)))
    ...     self.b = nnx.Param(jnp.zeros((dout,)))
    ...     self.count = Variable(jnp.array(0, dtype=jnp.uint32))
    ...
    ...   def increment(self):
    ...     self.count += 1
    ...
    ...   def __call__(self, x):
    ...     self.increment()
    ...     return x @ self.w + self.b
    ...
    >>> linear = StatefulLinear(3, 2, nnx.Rngs(0))
    >>> linear_state = nnx.split(linear)
    ...
    >>> @jax.jit
    ... def forward(x, linear_state):
    ...   y, linear_state = nnx.call(linear_state)(x)
    ...   return y, linear_state
    ...
    >>> x = jnp.ones((1, 3))
    >>> y, linear_state = forward(x, linear_state)
    >>> y, linear_state = forward(x, linear_state)
    ...
    >>> linear = nnx.merge(*linear_state)
    >>> linear.count.value
    Array(2, dtype=uint32)

  The proxy object returned by ``call`` supports indexing and attribute access
  to access nested methods. In the example below, the ``increment`` method indexing
  is used to call the ``increment`` method of the ``StatefulLinear`` module
  at the ``b`` key of a ``nodes`` dictionary.

    >>> class StatefulLinear(nnx.Module):
    ...   def __init__(self, din, dout, rngs):
    ...     self.w = nnx.Param(jax.random.uniform(rngs(), (din, dout)))
    ...     self.b = nnx.Param(jnp.zeros((dout,)))
    ...     self.count = nnx.Variable(jnp.array(0, dtype=jnp.uint32))
    ...
    ...   def increment(self):
    ...     self.count += 1
    ...
    ...   def __call__(self, x):
    ...     self.increment()
    ...     return x @ self.w + self.b
    ...
    >>> rngs = nnx.Rngs(0)
    >>> nodes = dict(
    ...   a=StatefulLinear(3, 2, rngs),
    ...   b=StatefulLinear(2, 1, rngs),
    ... )
    ...
    >>> node_state = nnx.split(nodes)
    >>> # use attribute access
    >>> _, node_state = nnx.call(node_state)['b'].increment()
    ...
    >>> nodes = nnx.merge(*node_state)
    >>> nodes['a'].count.value
    Array(0, dtype=uint32)
    >>> nodes['b'].count.value
    Array(1, dtype=uint32)
  """

  def pure_caller(accessor: DelayedAccessor, *args, **kwargs):
    node = merge(*graphdef_state)
    method = accessor(node)
    out = method(*args, **kwargs)
    return out, split(node)

  return CallableProxy(pure_caller)  # type: ignore


def iter_graph(node: tp.Any, /) -> tp.Iterator[tuple[PathParts, tp.Any]]:
  """Iterates over all nested nodes and leaves of the given graph node, including the current node.

  ``iter_graph`` creates a generator that yields path and value pairs, where
  the path is a tuple of strings or integers representing the path to the value from the
  root. Repeated nodes are visited only once. Leaves include static values.

  Example::
    >>> from flax import nnx
    >>> import jax.numpy as jnp
    ...
    >>> class Linear(nnx.Module):
    ...   def __init__(self, din, dout, *, rngs: nnx.Rngs):
    ...     self.din, self.dout = din, dout
    ...     self.w = nnx.Param(jax.random.uniform(rngs.next(), (din, dout)))
    ...     self.b = nnx.Param(jnp.zeros((dout,)))
    ...
    >>> module = Linear(3, 4, rngs=nnx.Rngs(0))
    >>> graph = [module, module]
    ...
    >>> for path, value in nnx.iter_graph(graph):
    ...   print(path, type(value).__name__)
    ...
    (0, '_object__nodes') frozenset
    (0, '_object__state') ObjectState
    (0, 'b') Param
    (0, 'din') int
    (0, 'dout') int
    (0, 'w') Param
    (0,) Linear
    () list
  """
  visited: set[int] = set()
  path_parts: PathParts = ()
  yield from _iter_graph(node, visited, path_parts)


def _iter_graph(
  node: tp.Any, visited: set[int], path_parts: PathParts
) -> tp.Iterator[tuple[PathParts, tp.Any]]:
  if is_node(node):
    if id(node) in visited:
      return
    visited.add(id(node))
    node_impl = get_node_impl(node)
    if node_impl is None and not (
      isinstance(node, Variable) or variablelib.is_mutable_array(node)
    ):
      raise RuntimeError(f'Unsupported type: {type(node)}, this is a bug.')
    assert node_impl is not None
    node_dict = node_impl.node_dict(node)
    for key, value in node_dict.items():
      yield from _iter_graph(value, visited, (*path_parts, key))

  yield path_parts, node


@jax.tree_util.register_static
@dataclasses.dataclass(frozen=True, slots=True)
class Static(tp.Generic[A]):
  """An empty pytree node that treats its inner value as static.
  ``value`` must define ``__eq__`` and ``__hash__``.
  """

  value: A


# ---------------------------------------------------------
# Pytree
# ---------------------------------------------------------
class GenericPytree: ...


from jax._src.tree_util import _registry as JAX_PYTREE_REGISTRY


def is_pytree_node(x: tp.Any) -> bool:
  if isinstance(x, Variable):
    return False
  elif type(x) in JAX_PYTREE_REGISTRY:
    return True
  elif isinstance(x, tuple):
    return True
  else:
    return False


def _key_path_to_key(key: tp.Any) -> Key:
  if isinstance(key, jax.tree_util.SequenceKey):
    return key.idx
  elif isinstance(
    key, (jax.tree_util.DictKey, jax.tree_util.FlattenedIndexKey)
  ):
    if not is_key_like(key.key):  # type: ignore[not-supported-yet]
      raise ValueError(
        f'Invalid key: {key.key}. May be due to its type not being hashable or comparable.'
      )
    return key.key
  elif isinstance(key, jax.tree_util.GetAttrKey):
    return key.name
  else:
    return str(key)


def jax_to_nnx_path(jax_path: tuple, /):
  return tuple(_key_path_to_key(part) for part in jax_path)


class IndexesPytreeDef(tp.NamedTuple):
  key_index: HashableMapping[Key, int]
  treedef: jax.tree_util.PyTreeDef


def _flatten_pytree(pytree: tp.Any):
  leaves, treedef = jax.tree_util.tree_flatten_with_path(
    pytree, is_leaf=lambda x: x is not pytree
  )
  nodes = [(_key_path_to_key(path[0]), value) for path, value in leaves]
  key_index = HashableMapping(
    {key: i for i, (key, _) in enumerate(nodes)}, copy=False
  )
  nodes.sort()  # sort by key
  return nodes, IndexesPytreeDef(key_index, treedef)


def _unflatten_pytree(
  nodes: tuple[tuple[Key, tp.Any], ...], metadata: IndexesPytreeDef
):
  # sort to original order
  sorted_nodes = sorted(nodes, key=lambda x: metadata.key_index[x[0]])
  pytree = metadata.treedef.unflatten(value for _, value in sorted_nodes)
  return pytree


PYTREE_NODE_IMPL = PytreeNodeImpl(
  type=GenericPytree,
  flatten=_flatten_pytree,
  unflatten=_unflatten_pytree,  # type: ignore
)

# common pytrees
# list
register_pytree_node_type(
  list,
  flatten=lambda x: (list(enumerate(x)), None),
  unflatten=lambda nodes, _: [value for _, value in nodes],  # type: ignore
)
# tuple
register_pytree_node_type(
  tuple,
  flatten=lambda x: (list(enumerate(x)), None),
  unflatten=lambda nodes, _: tuple(value for _, value in nodes),  # type: ignore
)
# dict
register_pytree_node_type(
  dict,
  flatten=lambda x: (sorted(x.items()), None),
  unflatten=lambda nodes, _: {key: value for key, value in nodes},  # type: ignore
)
# None
register_pytree_node_type(
  type(None),
  flatten=lambda x: ([], None),
  unflatten=lambda _, __: None,  # type: ignore
)
