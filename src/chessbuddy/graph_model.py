"""What-if tree model: the nodes, the lane layout, and the arithmetic behind
a node's Δ (see docs/whatif-graph-plan.md §3–§5).

Deliberately Qt-free — the geometry is plain numbers and the tree rules are
reasoned about (and tested) without a canvas.

Two identity rules that must stay separate (§3.1):

* a node is located in the tree by ``(parent.id, move.uci())``, so the same
  position reached two ways is **two nodes** (this is a tree, not a graph).
  "How did I get here" is then always unique, which is the only reason Δ
  means anything;
* the engine cache is keyed by **FEN**, so a transposition reuses a search
  result without reshaping the tree.

Both evals are stored White-relative (``eval_cp`` / ``eval_mate``) because a
node's Δ has to be measured across a colour change: the engine reports from
the side to move, and the parent's side to move is the opposite one.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import chess

# ------------------------------------------------------------------ geometry
LANE_H = 70           # vertical pitch between lanes (px)
PLY_W = 67            # horizontal pitch between plies (px)
NODE_W, NODE_H = 48, 38
NODE_RADIUS = 7

#: Plies laid out per engine line. Kept equal to ``analysis_panel._PV_LIMIT``:
#: the tree grows out of the PVs the panel already paid for, so the two views
#: must agree on how long a line is.
PV_LIMIT = 20

MATE_CP = 100_000     # what a mate is worth in centipawn arithmetic


def terminal(board: chess.Board) -> bool:
    """True for a position there is nothing left to search: mate, stalemate,
    a dead draw or a 75-move / fivefold claim that no longer needs a search."""
    return board.is_game_over(claim_draw=False)


@dataclass(eq=False)
class Node:
    """One move in the tree. It exists (its board can be shown) even when it
    has never been searched — that is the grey/lit distinction of §7.

    ``eq=False`` on purpose: nodes are mutable objects in a tree and are
    identified by their ``id``. A generated ``__eq__`` would compare the
    ``parent`` / ``children`` cycles field by field for any two distinct
    nodes, which is both wrong (two nodes at the same FEN are different
    nodes, §3.1) and potentially explosive.
    """

    id: str
    parent: "Node | None"
    move: chess.Move | None       # None only for the root
    fen: str
    san: str = ""
    source: str = "engine"        # "engine" | "user" | "game"
    note: str = ""                # human label, e.g. "引擎候选 #1"
    children: list["Node"] = field(default_factory=list)

    eval_cp: int | None = None    # White-relative centipawns
    eval_mate: int | None = None  # White-relative mate distance (signed)
    depth: int | None = None
    delta_cp: int | None = None   # loss vs. the parent's best move
    searched_at: float | None = None          # monotonic stamp (staleness)
    candidates: dict = field(default_factory=dict)   # multipv -> engine info

    searchable: bool = True
    #: ids of the children a *candidate* search laid out as lanes — never the
    #: continuation of a PV and never a branch grown by hand. This is the
    #: bookkeeping that makes both halves of the expand toggle possible:
    #: ``expanded`` is derived from it, and 收起候选 folds exactly these back.
    spawned: list[str] = field(default_factory=list)
    ply: int = 0                  # layout: distance from the root
    lane: int = 0                 # layout: which row

    @property
    def searched(self) -> bool:
        return self.searched_at is not None

    @property
    def expanded(self) -> bool:
        """Have its candidates been laid out?

        Derived from ``spawned`` rather than stored: killing a candidate lane
        by hand has to put the node back to expandable, otherwise the lanes
        could be removed one by one until none were left and the node would
        still claim to be expanded — with neither a ``+`` nor a ``−`` to
        offer, which is a dead end at the end of a context menu.
        """
        return bool(self.spawned)

    @property
    def lit(self) -> bool:
        """Has numbers to show (as opposed to searched-and-empty)."""
        return self.eval_cp is not None or self.eval_mate is not None

    @property
    def root(self) -> bool:
        return self.parent is None


class Tree:
    """Everything one what-if session is: nodes, layout, engine cache.

    The tree is anchored to one analysed position. ``anchor_fen`` changing
    means the whole tree is dropped (§3.2) — deliberately, so a branch can
    never end up hanging off an unrelated position.
    """

    def __init__(self, anchor_fen: str):
        board = chess.Board(anchor_fen)
        self.anchor_fen = anchor_fen
        self.root = Node(id="root", parent=None, move=None, fen=anchor_fen,
                         note="分析的起点", searchable=not terminal(board))
        self.nodes: dict[str, Node] = {self.root.id: self.root}
        #: position key (see :func:`cache_key`) -> {"lines", "depth", "t"}.
        #: Keyed by position, never by node: that is what makes a transposition
        #: free, and it cannot affect the tree's shape (§3.1).
        self.eval_cache: dict[str, dict] = {}

    # ------------------------------------------------------------- growth
    def child(self, parent: Node, uci: str) -> Node | None:
        """The existing child reached by ``uci``, if any."""
        return self.nodes.get(f"{parent.id}/{uci}")

    def add_child(self, parent: Node, uci: str, source: str = "user",
                  note: str = "") -> Node:
        """Add — or return — the child ``parent --uci-->``.

        Raises ValueError (from ``parse_uci``) on an illegal move; callers
        only ever pass moves the engine or the board already accepted.
        """
        nid = f"{parent.id}/{uci}"
        known = self.nodes.get(nid)
        if known is not None:
            return known
        board = chess.Board(parent.fen)
        move = board.parse_uci(uci)
        san = board.san(move)
        board.push(move)
        node = Node(id=nid, parent=parent, move=move, fen=board.fen(), san=san,
                    source=source, note=note, searchable=not terminal(board))
        self.nodes[nid] = node
        parent.children.append(node)
        return node

    def grow_line(self, parent: Node, pv: list[str], source: str = "engine",
                  note: str = "", chain_note: str = "",
                  limit: int = PV_LIMIT) -> list[Node]:
        """Lay a whole engine line out as a chain under ``parent``.

        A PV is one straight run of moves, so it becomes one lane: every node
        after the first is an only child and therefore inherits the lane
        (§4.2). Nodes come out unsearched — grey, no numbers (§7).

        ``note`` labels the head of the chain (the move that was actually
        chosen); ``chain_note`` labels its continuation, so the detail bar can
        say where a node deep in a line came from.
        """
        chain: list[Node] = []
        node = parent
        for i, uci in enumerate(pv[:limit]):
            try:
                node = self.add_child(node, uci, source=source,
                                      note=note if i == 0 else chain_note)
            except ValueError:
                break                      # a truncated PV is not fatal
            chain.append(node)
        return chain

    def path_to(self, node: Node) -> list[Node]:
        """Root-to-``node`` inclusive."""
        out: list[Node] = []
        while node is not None:
            out.append(node)
            node = node.parent
        out.reverse()
        return out

    def siblings(self, node: Node) -> list[Node]:
        return node.parent.children if node.parent is not None else [node]

    # ------------------------------------------------------------- pruning
    def subtree(self, node: Node) -> list[Node]:
        """``node`` and everything under it, parents before children."""
        out: list[Node] = []
        stack = [node]
        while stack:
            current = stack.pop()
            out.append(current)
            stack.extend(current.children)
        return out

    def remove_subtree(self, node: Node) -> list[Node]:
        """Kill ``node`` and every node under it; returns what was removed.

        The root is not removable — the tree is anchored to a position
        (§3.2), so a tree with no root has nothing left to mean — and this
        returns ``[]`` rather than raising, because the alternative is a
        caller-side check in every direction.

        The ``eval_cache`` entries are deliberately left alone: they are
        keyed by position, not by node (§3.1), so a branch that is killed
        and grown again costs no search.

        Only the *parent's* ``spawned`` needs filtering. Descendants of the
        killed node may hold ids of nodes that just died, but they go with
        it, and the parent is by construction the only survivor that can
        point into the removed set.
        """
        parent = node.parent
        if parent is None:
            return []
        removed = self.subtree(node)
        doomed = {n.id for n in removed}
        for gone in removed:
            self.nodes.pop(gone.id, None)
        parent.children.remove(node)
        parent.spawned = [cid for cid in parent.spawned if cid not in doomed]
        return removed

    def collapse(self, node: Node) -> list[Node]:
        """Fold a node's candidate lanes back in — an expand, undone.

        Exactly the children a candidate search laid out go (§5.2). A branch
        the user grew by hand is not ``spawned`` and survives, because the
        picture drawn by hand is not the engine's to fold; so is a PV
        continuation, which is not a candidate either.
        """
        removed: list[Node] = []
        for cid in list(node.spawned):
            child = self.nodes.get(cid)
            if child is not None:
                removed.extend(self.remove_subtree(child))
        node.spawned.clear()
        return removed

    # ----------------------------------------------------------- geometry
    def relayout(self) -> None:
        """Reassign ``ply`` / ``lane`` for the whole tree (§4.2).

        Two rules, and nothing else:

        1. the first child inherits its parent's lane, so an engine line reads
           as one straight row and the main line never drifts;
        2. a later child is inserted directly below the previous sibling's
           subtree, shifting every lane under that point down by one.

        The sibling *order* is fixed when the children are created and never
        revised: engine candidates go in in MultiPV order (best first, so the
        engine's first choice is the one that stays on the parent's row) and a
        user's branch is appended, because a move that has not been searched
        yet has no eval to sort by. Freezing it is what keeps the picture from
        reshuffling under the user's cursor.

        Rule 2 is what makes the viewport anchor free: a new sibling of the
        cursor's children lands at ``subtree_max(prev) + 1``, which is strictly
        **greater** than the cursor's own lane, so the cursor's scene
        coordinate is untouched and no compensation code is needed.

        Recomputing from the root each time is idempotent, so a re-layout can
        never drift the way an incremental "shift some lanes" pass would.
        """
        lanes: dict[str, int] = {}
        plies: dict[str, int] = {}

        def subtree_max(node: Node) -> int:
            best = lanes[node.id]
            for child in node.children:
                best = max(best, subtree_max(child))
            return best

        def place(node: Node, lane: int, ply: int) -> None:
            lanes[node.id] = lane
            plies[node.id] = ply
            for i, child in enumerate(node.children):
                if i == 0:
                    place(child, lane, ply + 1)          # main line stays put
                    continue
                insert_at = subtree_max(node.children[i - 1]) + 1
                for nid, value in list(lanes.items()):
                    if value >= insert_at:
                        lanes[nid] = value + 1
                place(child, insert_at, ply + 1)

        place(self.root, 0, 0)
        for nid, lane in lanes.items():
            node = self.nodes[nid]
            node.lane = lane
            node.ply = plies[nid]

    def lanes(self) -> int:
        """How many lanes the tree currently occupies (at least 1)."""
        return max((n.lane for n in self.nodes.values()), default=0) + 1

    def plies(self) -> int:
        return max((n.ply for n in self.nodes.values()), default=0)


# ------------------------------------------------------------------ evals
def white_cp(info: dict | None, stm: bool) -> int | None:
    """White-relative centipawns of an engine ``info`` dict (mate -> ±100000)."""
    if not info:
        return None
    mate = info.get("score_mate")
    if mate is not None:
        value = MATE_CP if mate > 0 else -MATE_CP
        return value if stm == chess.WHITE else -value
    cp = info.get("score_cp")
    if cp is None:
        return None
    return cp if stm == chess.WHITE else -cp


def white_mate(info: dict | None, stm: bool) -> int | None:
    """White-relative mate distance, or None when the line is not a mate."""
    if not info:
        return None
    mate = info.get("score_mate")
    if mate is None:
        return None
    return mate if stm == chess.WHITE else -mate


def delta_cp(parent: Node | None, node: Node) -> int | None:
    """Centipawns ``node``'s move gives up against the parent's best move.

    Both evals are White-relative, so the loss is measured from the parent's
    side to move — the side that had to choose — by flipping the difference
    for Black. ``None`` while either end is unsearched: a Δ needs a baseline.

    A negative result is possible (the move "beat" the parent's best line);
    it is search noise between two positions, and the 10cp noise band of §6.4
    is exactly what keeps it from being read as a real difference.
    """
    if parent is None or parent.eval_cp is None or node.eval_cp is None:
        return None
    sign = 1 if chess.Board(parent.fen).turn == chess.WHITE else -1
    return sign * (parent.eval_cp - node.eval_cp)


def stm_of(node: Node) -> bool:
    """Colour to move in ``node``'s position."""
    return chess.Board(node.fen).turn


def cache_key(fen: str) -> str:
    """The engine-cache key for a position: a FEN without its move counters.

    §3.1 keys the cache by position rather than by node so a transposition
    reuses a search instead of paying for it twice — and two paths to the same
    position almost never agree on the halfmove clock, so the counters have to
    go or the cache would only ever match a node against itself.
    """
    return " ".join(fen.split()[:4])


def move_text(node: Node) -> str:
    """'4… Nf6' — the move that reached ``node``, with its number prefix."""
    if node.parent is None:
        return "起始局面"
    board = chess.Board(node.parent.fen)
    prefix = (f"{board.fullmove_number}. " if board.turn == chess.WHITE
              else f"{board.fullmove_number}… ")
    return prefix + node.san


def can_expand(node: Node) -> bool:
    """Is there a free expansion waiting on this node?

    Only a searched node has candidates (one MultiPV search buys both the
    node's own eval *and* its three continuations — §6.1), and ``expanded``
    keeps a second click from re-laying the same moves out.

    Note that a node can have children *and* still be expandable: its children
    may be the continuation of a principal variation, which says nothing about
    whether a search of its own has been unfolded yet.
    """
    return bool(node.candidates) and not node.expanded and node.searchable


def can_collapse(node: Node) -> bool:
    """Is there something to fold back in?

    The other half of the toggle, and mutually exclusive with
    :func:`can_expand` by construction — laying the candidates out is exactly
    what stops the node being expandable — so a node offers one chip or the
    other and never both.
    """
    return bool(node.spawned)
