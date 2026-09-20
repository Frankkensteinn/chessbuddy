"""What-if view: the analysed position as a move tree you can walk, branch
and re-branch, on an infinite canvas (docs/whatif-graph-plan.md).

Three layers, top to bottom:

* :class:`WhatIfView` — the page. Top bar (view switch, fit / zoom / follow,
  the search status), a left dock with its *own* board on the shared cursor
  position, and the canvas. It owns the search scheduling and is the only
  place that talks to the engine.
* :class:`GraphCanvas` — pan / zoom / fit / smooth follow, the node items and
  the scene. "The viewport is never re-centred to keep a node still": an
  insertion in :meth:`Tree.relayout` cannot move the cursor node, so anchoring
  is free and the only thing that ever moves the view is *following the
  cursor*, which has its own switch (§4.2).
* :class:`NodeItem` / :class:`GraphScene` — the pixels, including the
  level-of-detail tiers of §4.3.

`QGraphicsItem` does not participate in the QSS, so every colour here is read
from :mod:`chessbuddy.theme` at paint time and ``refresh_theme`` has to force
a repaint (the same reason the board and the eval bar do).
"""
from __future__ import annotations

import time

import chess
import chess.pgn
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QKeySequence, QPainter, QPainterPath, QPen,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QFrame, QGraphicsItem, QGraphicsScene,
    QGraphicsView, QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton,
    QStyleOptionGraphicsItem, QVBoxLayout, QWidget,
)

from . import graph_model, theme
from .analysis_panel import EvalBar, _loss_label, _loss_tone
from .board_widget import BoardMode, BoardWidget
from .engine import EngineError

#: A click on an unsearched node buys this much search (§6.3). Shallow on
#: purpose: it lights the node up, and the depth is always shown next to it
#: so a 600ms result is never read as a verdict.
SEARCH_MS = 600
MULTIPV = 3                 # one search buys the node *and* its 3 candidates

#: §4.3 — what a node shows as it shrinks.
LOD_FULL = 1.0              # move + eval + depth dot
LOD_TEXT = 0.5              # move + eval
LOD_SHAPE = 0.3             # colours and shape only; below: a plain block

ZOOM_MIN, ZOOM_MAX = 0.15, 3.0
#: Zoom the tree opens at. Deliberately *not* "fit": an engine PV is ~20 plies
#: wide, and fitting that puts the nodes under the 0.5 LOD tier where their
#: text disappears — the opening picture would be unreadable. 「适应」 is one
#: click away for the overview.
ENTRY_ZOOM = 1.0
FOLLOW_MS = 200             # smooth follow, so a long jump is not a teleport
FOLLOW_MARGIN = 40          # px of slack before "it is already on screen"

#: Δ below this is the noise band of §6.4: two moves that differ by less than
#: this are not "two different choices", they are one search's jitter.
NOISE_CP = 10
MATE_LEVEL = 90_000         # |Δ| above this is a mate, not centipawns

#: The dock is a fixed width and its board is a square, so the eval bar beside
#: it is exactly as tall as the grid (see ``_build_dock``).
DOCK_W = 460
EVAL_W = 30
DOCK_GAP = 6
BOARD_PX = DOCK_W - EVAL_W - DOCK_GAP


# --------------------------------------------------------------- formatting
def eval_text(node: graph_model.Node) -> tuple[str, str]:
    """(chip text, tone) for a node's eval, always from White's perspective."""
    if node.eval_mate is not None:
        mate = node.eval_mate
        return f"M{abs(mate)}", "white" if mate > 0 else "black"
    if node.eval_cp is None:
        return "…", "even"
    cp = node.eval_cp
    tone = "white" if cp > 15 else "black" if cp < -15 else "even"
    return f"{cp / 100.0:+.2f}", tone


def delta_line(node: graph_model.Node) -> tuple[str, str]:
    """('Δ +0.03 · 与首选同价 · 引擎候选 #1', tone) for the detail bar."""
    if node.parent is None:
        return "Δ — · 分析的起点", "good"
    delta = graph_model.delta_cp(node.parent, node)
    if delta is None:
        return "Δ — · 父节点尚未搜索，没有比较基准", "good"
    amount = "Δ 杀棋" if abs(delta) >= MATE_LEVEL else f"Δ {delta / 100.0:+.2f}"
    if abs(delta) < NOISE_CP:
        # Same-price band: reusing the lowest existing tier would still say
        # "good move", which reads as a *choice* rather than as the noise it is.
        verdict = "与首选同价（噪声带）"
        tone = "good"
    else:
        verdict = _loss_label(delta)
        tone = _loss_tone(delta)
    tail = f" · {node.note}" if node.note else ""
    return f"{amount} · {verdict}{tail}", tone


def node_number_text(node: graph_model.Node) -> str:
    """The compact eval drawn inside a node ('' when there is nothing yet)."""
    if node.eval_mate is not None:
        return f"M{abs(node.eval_mate)}"
    if node.eval_cp is None:
        return ""
    return f"{node.eval_cp / 100.0:+.2f}"


def _visible(rect: QRectF, clip: QRectF) -> bool:
    """Does ``rect`` overlap the viewport? Inflated by the pen width first.

    ``QRectF.intersects`` answers *false* for a null rect (zero width **or**
    zero height), and a straight parent→child wire is exactly that: a child
    that inherits its parent's lane produces a path with zero height, and a
    vertical run would have zero width. Testing the raw bounding rect culled
    every wire on a main line while sparing the elbows — which is what made
    a fork look like the only thing that was connected to anything.

    Growing the rect by a hair costs one extra wire drawn just off screen and
    is the whole fix; the items themselves are never null and are unaffected.
    """
    return clip.intersects(rect.adjusted(-1.5, -1.5, 1.5, 1.5))


# -------------------------------------------------------------- node items
class NodeItem(QGraphicsItem):
    """One node: a rounded box with the move, its eval and a depth dot.

    The action chip hangs *below* the box — inside the bounding rect, in
    the gap the lane pitch leaves — so it never collides with the wire that
    leaves the right edge. There is **one** chip and its glyph is the state
    (§5.2): ``+`` while the node's candidates can be laid out, ``−`` once
    they are out and can be folded back. One chip rather than two because the
    two actions are mutually exclusive by construction — see
    :func:`graph_model.can_collapse` — and a permanently half-disabled pair
    of buttons under every node would read as decoration.

    Right click is the node's menu: expand / collapse / **delete this branch
    and everything under it** (§5.4). Deleting is the only irreversible thing
    in this view, which is why it lives behind a menu and has no key.
    """

    MARKER = 11.0
    BOTTOM = graph_model.NODE_H + MARKER + 5.0

    def __init__(self, node: graph_model.Node, canvas: "GraphCanvas"):
        super().__init__()
        self.node = node
        self._canvas = canvas
        self._hover = False
        self._cursor = False
        self._on_path = False
        self.setAcceptHoverEvents(True)
        self.sync_pos()

    # ------------------------------------------------------------- state
    def sync_pos(self) -> None:
        x = self.node.ply * graph_model.PLY_W
        y = self.node.lane * graph_model.LANE_H
        self.setPos(x, y)

    def set_state(self, on_path: bool, cursor: bool) -> None:
        if (on_path, cursor) == (self._on_path, self._cursor):
            return
        self._on_path, self._cursor = on_path, cursor
        self.setZValue(2 if cursor else (1 if on_path else 0))
        self.update()

    def boundingRect(self) -> QRectF:
        return QRectF(0.0, 0.0, graph_model.NODE_W, self.BOTTOM)

    def marker_rect(self) -> QRectF:
        return QRectF((graph_model.NODE_W - self.MARKER) / 2.0,
                      graph_model.NODE_H + 3.0, self.MARKER, self.MARKER)

    def box(self) -> QRectF:
        return QRectF(0.0, 0.0, graph_model.NODE_W, graph_model.NODE_H)

    def marker_action(self) -> str | None:
        """'expand' / 'collapse' / None — which half of the toggle applies."""
        if graph_model.can_collapse(self.node):
            return "collapse"
        if graph_model.can_expand(self.node):
            return "expand"
        return None

    def _marker_live(self) -> bool:
        """Chips are a cursor/hover affordance, and the hit test has to agree
        with the paint or a click would land on something invisible."""
        return self._cursor or self._hover

    # -------------------------------------------------------------- input
    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        action = self.marker_action()
        if action is not None and self._marker_live() \
                and self.marker_rect().contains(event.pos()):
            # Acting on a node should also put the cursor on it, or the dock
            # board would keep showing one node while another one changes.
            self._canvas.select_node(self.node)
            self._canvas.setFocus(Qt.FocusReason.MouseFocusReason)
            if action == "collapse":
                self._canvas.host.collapse_node(self.node)
            else:
                self._canvas.host.expand_node(self.node)
        else:
            self._canvas.select_node(self.node)
        event.accept()

    def contextMenuEvent(self, event) -> None:
        if self._canvas.tree() is None:
            event.ignore()
            return
        # The menu's verbs act on what was right-clicked, so land the cursor
        # there first and let the dock board confirm which node this is.
        self._canvas.select_node(self.node)
        menu, verbs = self.node_menu()
        verb = verbs.get(menu.exec(event.screenPos()))
        if verb == "kill":
            self._canvas.host.kill_branch(self.node)
        elif verb == "collapse":
            self._canvas.host.collapse_node(self.node)
        elif verb == "expand":
            self._canvas.host.expand_node(self.node)
        event.accept()

    def node_menu(self) -> tuple[QMenu, dict]:
        """(menu, {action: verb}) for this node.

        Built separately from the ``exec`` so the *offer* — which verbs apply
        to this node and which are greyed out — can be read without a modal
        menu blocking the caller (§5.4: the root has no delete).
        """
        node = self.node
        menu = QMenu(self._canvas)
        expand = menu.addAction("＋ 展开候选")
        expand.setEnabled(graph_model.can_expand(node))
        collapse = menu.addAction("− 收起候选（保留本节点）")
        collapse.setEnabled(graph_model.can_collapse(node))
        menu.addSeparator()
        kill = menu.addAction("✕ 删除该分支及其全部子节点")
        kill.setEnabled(node.parent is not None)
        if node.parent is None:
            kill.setToolTip("根节点不删：树锚定在它上面")
        return menu, {expand: "expand", collapse: "collapse", kill: "kill"}

    def hoverEnterEvent(self, event) -> None:
        self._hover = True
        self.update()

    def hoverLeaveEvent(self, event) -> None:
        self._hover = False
        self.update()

    # ------------------------------------------------------------ painting
    def paint(self, painter: QPainter, option, widget=None) -> None:
        lod = QStyleOptionGraphicsItem.levelOfDetailFromTransform(painter.worldTransform())
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        try:
            self._paint(painter, lod)
        finally:
            painter.restore()

    def _paint(self, painter: QPainter, lod: float) -> None:
        node = self.node
        box = self.box()

        if lod < LOD_SHAPE:
            # Below the smallest text tier only the *shape* still carries
            # information: where the forks are, how long a chain runs.
            painter.setPen(Qt.PenStyle.NoPen)
            lit = node.lit
            painter.setBrush(QColor(theme.NODE_TEXT if lit else theme.NODE_BORDER))
            painter.drawRoundedRect(box, 2.0, 2.0)
            return

        lit = node.lit
        path = QPainterPath()
        path.addRoundedRect(box, graph_model.NODE_RADIUS, graph_model.NODE_RADIUS)

        if not node.searchable:
            fill = QColor(theme.NODE_TERMINAL)          # mate / stalemate
        else:
            fill = QColor(theme.NODE_BG)
            if not lit:
                # A prediction is hollow — but only while the text that says
                # so is legible. Once the tier drops to shapes-only the body
                # has to carry the reading, so it gets a fill back.
                fill.setAlpha(0 if lod >= LOD_TEXT else 95)
        border = QColor(theme.NODE_BORDER)
        if not lit:
            border.setAlpha(120)
        if self._cursor:
            pen = QPen(QColor(theme.ACCENT), 1.5)
        else:
            pen = QPen(border, 1.0)
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        painter.drawPath(path)

        # left edge stripe: where the move came from
        if node.parent is not None:
            stripe = QColor(theme.NODE_ENGINE if node.source == "engine" else theme.ACCENT)
            if not lit and node.source != "engine":
                stripe.setAlpha(150)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(stripe)
            painter.drawRoundedRect(
                QRectF(3.0, 6.0, 2.5, graph_model.NODE_H - 12.0), 1.2, 1.2)

        if lod >= LOD_TEXT and node.san:
            font = QFont(self._canvas.font())
            font.setPointSizeF(8.0)
            font.setBold(lit)
            painter.setFont(font)
            painter.setPen(QColor(theme.NODE_TEXT if lit else theme.NODE_FAINT))
            top = QRectF(2.0, 3.0, graph_model.NODE_W - 4.0, 15.0)
            painter.drawText(top, Qt.AlignmentFlag.AlignCenter, node.san)

        if lod >= LOD_FULL:
            number = node_number_text(node)
            if number:
                font = QFont(self._canvas.font())
                font.setPointSizeF(7.0)
                painter.setFont(font)
                painter.setPen(QColor(theme.NODE_TEXT))
                # The root has no move to label, so its eval takes the whole
                # box instead of sitting under an empty first line.
                low = (QRectF(2.0, 3.0, graph_model.NODE_W - 4.0,
                              graph_model.NODE_H - 6.0) if not node.san
                       else QRectF(2.0, 17.0, graph_model.NODE_W - 4.0, 14.0))
                painter.drawText(low, Qt.AlignmentFlag.AlignCenter, number)
            if node.depth:
                # A dot whose size grows with depth: a 600ms node and a
                # fully-analyzed one must not look equally authoritative.
                r = 1.4 + 1.6 * min(node.depth, 24) / 24.0
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(theme.NODE_FAINT))
                painter.drawEllipse(QPointF(graph_model.NODE_W - 6.0, 6.0), r, r)

            if node.searchable and not lit:
                # It exists but has never been searched: say so in the one
                # place the eye is already on, rather than silently showing
                # nothing.
                font = QFont(self._canvas.font())
                font.setPointSizeF(6.5)
                painter.setFont(font)
                painter.setPen(QColor(theme.NODE_FAINT))
                painter.drawText(QRectF(2.0, 17.0, graph_model.NODE_W - 4.0, 14.0),
                                 Qt.AlignmentFlag.AlignCenter, "…")

        if lod >= LOD_TEXT and self._marker_live():
            action = self.marker_action()
            if action is not None:
                self._paint_marker(painter, action)

    def _paint_marker(self, painter: QPainter, action: str) -> None:
        rect = self.marker_rect()
        color = QColor(theme.ACCENT if self._hover else theme.MUTED)
        painter.setPen(QPen(color, 1.0))
        fill = QColor(theme.BG_PANEL)
        fill.setAlpha(235)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 3.0, 3.0)
        font = QFont(self._canvas.font())
        font.setPointSizeF(7.5)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                         "−" if action == "collapse" else "+")


class GraphScene(QGraphicsScene):
    """Draws the wires and the current-path band behind the node items."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tree: graph_model.Tree | None = None
        self.path_ids: set[str] = set()

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.fillRect(rect, QColor(theme.GRAPH_BG))
        tree = self.tree
        if tree is None:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        nodes = tree.nodes.values()

        # 1. the band under the current path: "this is where you are"
        band = QColor(theme.ACCENT_TINT)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(band)
        for node in nodes:
            if node.id not in self.path_ids:
                continue
            box = self._box(node)
            if not _visible(box, rect):
                continue
            painter.drawRoundedRect(box.adjusted(-3.0, -3.0, 3.0, 3.0), 9.0, 9.0)

        # 2. parent -> child wires
        for node in nodes:
            for child in node.children:
                path = self._wire(node, child)
                if path is None or not _visible(path.boundingRect(), rect):
                    continue
                on_path = node.id in self.path_ids and child.id in self.path_ids
                color = QColor(theme.ACCENT if on_path else theme.GRAPH_EDGE)
                if on_path:
                    color.setAlpha(190)
                painter.setPen(QPen(color, 2.4 if on_path else 1.6,
                                    Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                    Qt.PenJoinStyle.RoundJoin))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)

    @staticmethod
    def _box(node: graph_model.Node) -> QRectF:
        return QRectF(node.ply * graph_model.PLY_W, node.lane * graph_model.LANE_H,
                      graph_model.NODE_W, graph_model.NODE_H)

    @classmethod
    def _wire(cls, parent: graph_model.Node, child: graph_model.Node) -> QPainterPath | None:
        """Right edge of the parent to the left edge of the child.

        A child that inherits its parent's lane gets a straight run; anything
        else leaves the parent, turns once, and comes back in — the elbow
        passes through the empty cells before the lane starts, which is the
        "this is a fork" signal of §4.1.
        """
        p, c = cls._box(parent), cls._box(child)
        x1, y1 = p.right(), p.center().y()
        x2, y2 = c.left(), c.center().y()
        path = QPainterPath(QPointF(x1, y1))
        if abs(y1 - y2) < 0.5:
            path.lineTo(x2, y2)
        else:
            mid = x1 + max(9.0, (x2 - x1) * 0.5)
            path.lineTo(mid, y1)
            path.lineTo(mid, y2)
            path.lineTo(x2, y2)
        return path


class GraphCanvas(QGraphicsView):
    """The infinite canvas: pan by dragging the background, wheel to scroll,
    Cmd/Ctrl+wheel (or +/-) to zoom, 0 to fit."""

    zoomChanged = pyqtSignal(float)

    def __init__(self, host: "WhatIfView"):
        super().__init__(host)
        self.host = host
        self._scene = GraphScene(self)
        self.setScene(self._scene)
        self._items: dict[str, NodeItem] = {}
        self._cursor: graph_model.Node | None = None
        self._zoom = 1.0
        self._pan_from = None
        self._follow: dict | None = None
        self._follow_timer = QTimer(self)
        self._follow_timer.setInterval(16)          # ~60 fps
        self._follow_timer.timeout.connect(self._on_follow_tick)

        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setRenderHints(QPainter.RenderHint.Antialiasing
                            | QPainter.RenderHint.TextAntialiasing
                            | QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        # An infinite canvas with scrollbars reads as a small window onto a
        # big sheet; panning and the wheel already cover navigation.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ------------------------------------------------------------- content
    def set_tree(self, tree: graph_model.Tree | None) -> None:
        self._items.clear()          # never touch the wrappers after clear()
        self._scene.clear()
        self._scene.tree = tree
        self._scene.path_ids = set()
        self._cursor = None
        if tree is None:
            self._scene.setSceneRect(QRectF(-600, -600, 1200, 1200))
            return
        for node in tree.nodes.values():
            self._add_item(node)
        self.sync()

    def _add_item(self, node: graph_model.Node) -> NodeItem:
        item = NodeItem(node, self)
        self._scene.addItem(item)
        self._items[node.id] = item
        return item

    def item_for(self, node: graph_model.Node) -> NodeItem | None:
        return self._items.get(node.id)

    def tree(self) -> graph_model.Tree | None:
        return self._scene.tree

    def remove_nodes(self, ids: set[str]) -> None:
        """Drop the items of nodes that are no longer in the tree.

        Must run **before** anything walks ``_items`` again: an item whose
        C++ object the scene has released is not something ``set_state`` can
        be called on. Dropping the last Python reference is what deletes it,
        which is why ``clear()``-style bulk teardown is done by rebuilding
        the whole scene instead (see :meth:`set_tree`).

        The cursor is cleared when it pointed into the removed set so no
        later redraw can ask for an item that is gone; the host immediately
        selects the node the user should be looking at instead.
        """
        for nid in ids:
            item = self._items.pop(nid, None)
            if item is not None:
                self._scene.removeItem(item)
        if self._cursor is not None and self._cursor.id in ids:
            self._cursor = None

    def sync(self) -> None:
        """Re-read the model after it grew: items, positions, scene rect.

        Deliberately does **not** touch the view transform. An insertion can
        only push lanes *below* the cursor (§4.2), so the cursor's scene
        position is already correct and nothing has to be compensated — no
        "re-centre after re-layout" step exists here to get wrong.
        """
        tree = self._scene.tree
        if tree is None:
            return
        tree.relayout()
        for node in tree.nodes.values():
            item = self._items.get(node.id)
            if item is None:
                item = self._add_item(node)
            item.sync_pos()
        self._refresh_states()
        self._update_scene_rect()
        self._scene.update()

    def _update_scene_rect(self) -> None:
        rect = self._scene.itemsBoundingRect()
        if rect.isEmpty():
            rect = QRectF(0, 0, 1, 1)
        pad = max(600.0, rect.width() * 0.25)
        self._scene.setSceneRect(rect.adjusted(-pad, -pad, pad, pad))

    def _refresh_states(self) -> None:
        tree = self._scene.tree
        if tree is None:
            return
        path = set()
        if self._cursor is not None:
            path = {node.id for node in tree.path_to(self._cursor)}
        if path != self._scene.path_ids:
            self._scene.path_ids = path
            self._scene.update()
        for nid, item in self._items.items():
            item.set_state(nid in path, self._cursor is not None and nid == self._cursor.id)

    # ------------------------------------------------------------- cursor
    def select_node(self, node: graph_model.Node, inform: bool = True) -> None:
        self._cursor = node
        self._refresh_states()
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if inform:
            self.host.node_selected(node)

    def cursor(self) -> graph_model.Node | None:
        return self._cursor

    # -------------------------------------------------------- transform
    def zoom_factor(self) -> float:
        return self._zoom

    def zoom_by(self, factor: float) -> None:
        target = self._zoom * factor
        if target < ZOOM_MIN:
            factor = ZOOM_MIN / self._zoom
        elif target > ZOOM_MAX:
            factor = ZOOM_MAX / self._zoom
        if abs(factor - 1.0) < 1e-9:
            return
        self.scale(factor, factor)
        self._zoom = self.transform().m11()
        self.zoomChanged.emit(self._zoom)

    def fit(self) -> None:
        rect = self._scene.itemsBoundingRect()
        if rect.isEmpty():
            return
        pad = max(30.0, rect.width() * 0.04)
        self.fitInView(rect.adjusted(-pad, -pad, pad, pad),
                       Qt.AspectRatioMode.KeepAspectRatio)
        self._zoom = self.transform().m11()
        self.zoomChanged.emit(self._zoom)

    def reset_zoom(self, node: graph_model.Node | None = None,
                   zoom: float = ENTRY_ZOOM, bias: float = 0.22) -> None:
        """Come back to a readable zoom, centred on ``node`` (the entry state).

        ``bias`` nudges the node left of centre so most of the viewport is
        spent on the moves that have not happened yet.
        """
        self.stop_follow()
        self.resetTransform()
        self._zoom = max(ZOOM_MIN, min(zoom, ZOOM_MAX))
        self.scale(self._zoom, self._zoom)
        self.zoomChanged.emit(self._zoom)
        if node is None:
            return
        item = self._items.get(node.id)
        if item is None:
            return
        view = self.mapToScene(self.viewport().rect()).boundingRect()
        centre = item.mapToScene(item.box()).boundingRect().center()
        self.centerOn(QPointF(centre.x() + view.width() * bias, centre.y()))

    # ------------------------------------------------------------ follow
    def follow_node(self, node: graph_model.Node, animate: bool = True) -> None:
        """Keep ``node`` on screen — the only thing that moves the viewport."""
        item = self._items.get(node.id)
        if item is None:
            return
        self.center_towards(item.mapToScene(item.box()).boundingRect().center(), animate)

    def center_towards(self, point: QPointF, animate: bool = True) -> None:
        visible = self.viewport().rect().adjusted(
            FOLLOW_MARGIN, FOLLOW_MARGIN, -FOLLOW_MARGIN, -FOLLOW_MARGIN)
        if visible.contains(self.mapFromScene(point)):
            return
        if not animate:
            self.centerOn(point)
            return
        start = self.mapToScene(self.viewport().rect()).boundingRect().center()
        self._follow = {"t0": time.monotonic(), "dur": FOLLOW_MS / 1000.0,
                        "a": start, "b": QPointF(point)}
        self._follow_timer.start()

    def stop_follow(self) -> None:
        self._follow = None
        self._follow_timer.stop()

    def _on_follow_tick(self) -> None:
        follow = self._follow
        if follow is None:
            self._follow_timer.stop()
            return
        t = min(1.0, (time.monotonic() - follow["t0"]) / follow["dur"])
        eased = 1.0 - (1.0 - t) ** 3             # ease-out: settle, don't snap
        a, b = follow["a"], follow["b"]
        self.centerOn(QPointF(a.x() + (b.x() - a.x()) * eased,
                              a.y() + (b.y() - a.y()) * eased))
        if t >= 1.0:
            self._follow = None
            self._follow_timer.stop()

    # -------------------------------------------------------------- mouse
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton \
                and self.itemAt(event.position().toPoint()) is None:
            self.stop_follow()
            self._pan_from = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._pan_from is not None:
            pos = event.position().toPoint()
            delta = pos - self._pan_from
            self._pan_from = pos
            for bar, step in ((self.horizontalScrollBar(), delta.x()),
                              (self.verticalScrollBar(), delta.y())):
                bar.setValue(bar.value() - step)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._pan_from is not None:
            self._pan_from = None
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:
        mods = event.modifiers()
        delta = event.angleDelta().y() or event.angleDelta().x()
        if mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            self.zoom_by(1.0015 ** delta)
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - delta)
        else:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() - delta)
        event.accept()

    # ------------------------------------------------------------- theme
    def refresh_theme(self) -> None:
        self._scene.update()
        self.viewport().update()


# ------------------------------------------------------------------ the page
class WhatIfView(QWidget):
    """The whole what-if page. ``exited`` asks the window for the board page."""

    exited = pyqtSignal()

    def __init__(self, engine, renderer=None, parent: QWidget | None = None):
        super().__init__(parent)
        self._engine = engine
        self._tree: graph_model.Tree | None = None
        self._cursor: graph_model.Node | None = None
        self._searching: graph_model.Node | None = None      # job in flight
        self._pending: graph_model.Node | None = None        # last click wins
        self._active = False
        self._followed = False

        # Its own board, sharing the window's piece renderers (one SVG parse).
        self._board = BoardWidget(renderer=renderer)
        self._board.set_mode(BoardMode.BRANCH)
        self._board.setToolTip("从走子方拖一个合法着法，就从这里长出一条新支")
        self._board.branchMove.connect(self._on_branch_move)
        self._eval_bar = EvalBar(Qt.Orientation.Vertical)
        self._canvas = GraphCanvas(self)
        self._canvas.zoomChanged.connect(self._on_zoom)

        self._build_ui()
        self._build_shortcuts()
        self._engine.idle.connect(self._on_engine_idle)
        self._reset_detail()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(10)
        outer.addLayout(self._build_topbar())

        body = QHBoxLayout()
        body.setSpacing(10)
        body.addWidget(self._build_dock(), 0)
        body.addWidget(self._canvas, 1)
        outer.addLayout(body, 1)

    def _build_topbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self._board_seg = QPushButton("♟ 棋盘")
        self._graph_seg = QPushButton("⑂ 图表")
        group = QButtonGroup(self)
        group.setExclusive(True)
        for button in (self._board_seg, self._graph_seg):
            button.setObjectName("segBtn")
            button.setCheckable(True)
            group.addButton(button)
            bar.addWidget(button)
        self._graph_seg.setChecked(True)
        self._board_seg.setToolTip("Back to the board view (Esc)")
        self._board_seg.clicked.connect(self._on_board_seg)

        self._fit_btn = QPushButton("适应")
        self._fit_btn.setToolTip("Zoom to fit the whole tree (0)")
        self._fit_btn.clicked.connect(lambda: self._canvas.fit())
        bar.addWidget(self._fit_btn)

        self._zoom_label = QLabel("100%")
        self._zoom_label.setObjectName("statusLabel")
        self._zoom_label.setFixedWidth(42)
        bar.addWidget(self._zoom_label)

        self._follow_check = QCheckBox("跟随")
        self._follow_check.setChecked(True)
        self._follow_check.setToolTip(
            "跟随：光标移到哪个节点，视口就自动平移过去，让那个节点留在屏幕里。\n"
            "关掉它就能自由平移、不会被拉回来。它只管「光标移动」——泳道插入时\n"
            "图整体上下的位移是布局本身，跟这个开关无关。"
        )
        self._follow_check.toggled.connect(self._on_follow_toggled)
        bar.addWidget(self._follow_check)

        self._dock_btn = QPushButton("收起棋盘")
        self._dock_btn.setToolTip("Hide the dock so the graph fills the window")
        self._dock_btn.clicked.connect(self._toggle_dock)
        bar.addWidget(self._dock_btn)

        self._expand_btn = QPushButton("＋ 展开候选")
        self._expand_btn.setToolTip(self._EXPAND_TIP)
        self._expand_btn.clicked.connect(self.expand_cursor)
        bar.addWidget(self._expand_btn)

        self._pgn_btn = QPushButton("复制 PGN")
        self._pgn_btn.setToolTip("Copy the path from the root to the cursor as PGN")
        self._pgn_btn.clicked.connect(self.copy_pgn)
        bar.addWidget(self._pgn_btn)

        bar.addStretch(1)
        self._status = QLabel("")
        self._status.setObjectName("statusLabel")
        bar.addWidget(self._status)
        return bar

    def _build_dock(self) -> QWidget:
        """The left dock: its own board on the shared cursor position, plus
        the node detail bar. A second ``BoardWidget`` rather than reparenting
        the window's — reparenting would blank page 0 and re-plumb its
        signals, which costs far more than one extra widget."""
        self._dock = QWidget()
        self._dock.setFixedWidth(DOCK_W)
        lay = QVBoxLayout(self._dock)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # The board is fixed to a square so the eval bar beside it is exactly
        # as tall as the drawn board — otherwise the widget stretches to the
        # dock height while the grid stays centred, and the two stop lining up.
        row = QHBoxLayout()
        row.setSpacing(DOCK_GAP)
        self._board.setFixedSize(BOARD_PX, BOARD_PX)
        row.addWidget(self._eval_bar, 0)
        row.addWidget(self._board, 0)
        lay.addLayout(row)

        head = QHBoxLayout()
        head.setSpacing(8)
        self._move_label = QLabel("—")
        self._move_label.setObjectName("nodeMove")
        head.addWidget(self._move_label)
        self._chip = QLabel("…")
        self._chip.setObjectName("evalChip")
        head.addWidget(self._chip)
        self._depth_label = QLabel("")
        self._depth_label.setObjectName("lineDepth")
        head.addWidget(self._depth_label)
        head.addStretch(1)
        lay.addLayout(head)

        self._delta_label = QLabel("")
        self._delta_label.setObjectName("blunderResult")
        self._delta_label.setWordWrap(True)
        lay.addWidget(self._delta_label)

        hint = QLabel("拖子建分支 · Enter 展开/收起候选 · ↑↓ 换支 · "
                      "右键节点可删整个分支 · Esc 回棋盘")
        hint.setObjectName("nodeHint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addStretch(1)
        return self._dock

    def _build_shortcuts(self) -> None:
        # WidgetWithChildren: these only fire while this page owns the focus,
        # so they cannot steal keys from the board view (which is hidden then
        # anyway) and need no manual enabling.
        for key, slot in (
            (Qt.Key.Key_Left, self._nav_back),
            (Qt.Key.Key_Right, self._nav_forward),
            (Qt.Key.Key_Up, lambda: self._nav_sibling(-1)),
            (Qt.Key.Key_Down, lambda: self._nav_sibling(1)),
            (Qt.Key.Key_Home, self._nav_home),
            (Qt.Key.Key_Return, self.expand_cursor),
            (Qt.Key.Key_Enter, self.expand_cursor),
            (Qt.Key.Key_Space, self.expand_cursor),
            (Qt.Key.Key_Plus, lambda: self._canvas.zoom_by(1.2)),
            (Qt.Key.Key_Equal, lambda: self._canvas.zoom_by(1.2)),
            (Qt.Key.Key_Minus, lambda: self._canvas.zoom_by(1 / 1.2)),
            (Qt.Key.Key_0, lambda: self._canvas.fit()),
        ):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)
        # Esc has to work even when the dock board holds the focus, and it
        # must not fire while the page is hidden — hence WindowShortcut with
        # an explicit enable.
        self._esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc.setContext(Qt.ShortcutContext.WindowShortcut)
        self._esc.activated.connect(self._on_board_seg)
        self._esc.setEnabled(False)

    # ------------------------------------------------------------ entering
    def enter(self, anchor_fen: str, lines: dict, flipped: bool = False) -> None:
        """Show the tree of ``anchor_fen``, growing it from ``lines`` on the
        way in — the three engine lines become three lanes for free (§2)."""
        if self._tree is None or self._tree.anchor_fen != anchor_fen:
            self._tree = graph_model.Tree(anchor_fen)
            self._plant(lines)
            self._canvas.set_tree(self._tree)
            self._cursor = self._tree.root
        self._board.set_flipped(flipped)
        self._board.set_mode(BoardMode.BRANCH)
        self._graph_seg.setChecked(True)
        self._followed = False
        self._canvas.select_node(self._cursor or self._tree.root)
        self._canvas.reset_zoom(self._cursor or self._tree.root)
        self._set_status(
            f"根节点 + {len(self._tree.nodes) - 1} 个预测节点 · "
            "点节点看该局面，Enter 展开/收起候选，拖子长出你自己的分支，"
            "右键节点可删掉整个分支"
        )

    def _plant(self, lines: dict) -> None:
        """Turn the panel's finished analysis into the opening picture."""
        tree = self._tree
        assert tree is not None
        if not lines:
            return
        self._apply_lines(tree.root, lines, t=time.monotonic())
        for pv_no in sorted(lines):
            pv = list(lines[pv_no].get("pv") or [])[:graph_model.PV_LIMIT]
            if pv:
                self._spawn(tree, tree.root, pv_no, pv)
        # The root's three lanes *are* its candidates: there is nothing left
        # to unfold there, and offering it would only relist the same moves.
        # Recording the heads above is what says so (``expanded`` is derived
        # from ``spawned``) — and it is also what lets 收起候选 fold the
        # opening picture back to a bare root.

    def on_anchor_lost(self) -> None:
        """The analysed position changed: the tree goes with it (§3.2)."""
        self._tree = None
        self._cursor = None
        self._searching = None
        self._pending = None
        self._canvas.stop_follow()
        self._canvas.set_tree(None)
        self._reset_detail()
        self._set_status("")

    def set_active(self, on: bool) -> None:
        self._active = on
        self._esc.setEnabled(on)
        if on:
            self._graph_seg.setChecked(True)
            self._canvas.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self._pending = None         # no chained searches while hidden

    def is_active(self) -> bool:
        return self._active

    def cursor_fen(self) -> str | None:
        return self._cursor.fen if self._cursor is not None else None

    def refresh_theme(self) -> None:
        self._canvas.refresh_theme()
        self._board.update()
        self._eval_bar.update()

    def shutdown(self) -> None:
        self._canvas.stop_follow()

    # ------------------------------------------------------------- cursor
    def node_selected(self, node: graph_model.Node) -> None:
        self._cursor = node
        self._show(node)
        if node.searchable:
            self._request(node)
        if self._followed and self._follow_check.isChecked():
            self._canvas.follow_node(node)
        self._followed = True

    def _on_follow_toggled(self, on: bool) -> None:
        """「跟随」 reads as a mystery control until it has visibly done
        something once (§5.3): switching it on pulls the cursor node back into
        view right then — if it is off screen, which is the only case where
        following has anything to do — and says in the status bar what it is
        for. Switching it off stops an animation in flight, so the view can be
        panned without being dragged back mid-gesture.
        """
        if on:
            self._set_status("跟随开启：切到哪个节点，视口就跟过去，让它留在屏幕里")
            if self._cursor is not None:
                self._canvas.follow_node(self._cursor)
                self._followed = True
        else:
            self._canvas.stop_follow()
            self._set_status("跟随关闭：视口只在你平移 / 缩放 / 按「适应」时改变")

    def _show(self, node: graph_model.Node) -> None:
        """Mirror the node onto the dock board, the eval bar and the detail
        bar. The board is instant (the position is derived, not searched);
        the numbers are not, and say so when they are not there yet."""
        try:
            self._board.set_fen(node.fen)
        except ValueError:
            return
        if node.move is not None:
            self._board.set_last_move(node.move.from_square, node.move.to_square)
        else:
            self._board.clear_last_move()
        self._board.clear_arrow()
        self._board.set_move_overlays(self._overlays(node))

        if node.lit:
            stm = graph_model.stm_of(node)
            sign = 1 if stm == chess.WHITE else -1
            info = {
                "score_cp": None if node.eval_mate is not None
                else sign * (node.eval_cp or 0),
                "score_mate": None if node.eval_mate is None
                else sign * node.eval_mate,
            }
            self._eval_bar.set_eval(info, stm)
        else:
            self._eval_bar.set_unknown()

        self._move_label.setText(graph_model.move_text(node))
        text, tone = eval_text(node)
        self._chip.setText(text)
        self._chip.setProperty("tone", tone)
        theme.repolish(self._chip)
        self._depth_label.setText(f"d{node.depth}" if node.depth else "")

        # Δ is recomputed here and not merely read off the node: the parent may
        # have been searched *after* this node was, which is exactly when the
        # number finally becomes meaningful.
        node.delta_cp = graph_model.delta_cp(node.parent, node)
        line, tone = delta_line(node)
        self._delta_label.setText(line)
        self._delta_label.setProperty("tone", tone)
        theme.repolish(self._delta_label)
        self._sync_expand_btn(node)

    _EXPAND_TIP = ("把光标节点的三个引擎候选铺成泳道（Enter）。免费：着法来自"
                   "点亮它的那次搜索。")
    _COLLAPSE_TIP = ("把光标节点已铺开的候选收回去（Enter）。节点本身、它所在的"
                     "泳道、以及你手建的分支都保留。")

    def _sync_expand_btn(self, node: graph_model.Node | None) -> None:
        """The button is the other half of the marker's toggle, so its label
        has to follow the state: on an expanded node there is nothing left to
        expand and the only meaningful action is to fold the lanes back in."""
        if node is not None and graph_model.can_collapse(node):
            self._expand_btn.setText("− 收起候选")
            self._expand_btn.setEnabled(True)
            self._expand_btn.setToolTip(self._COLLAPSE_TIP)
        else:
            self._expand_btn.setText("＋ 展开候选")
            self._expand_btn.setEnabled(node is not None
                                        and graph_model.can_expand(node))
            self._expand_btn.setToolTip(self._EXPAND_TIP)

    @staticmethod
    def _overlays(node: graph_model.Node) -> list[tuple[int, int, str]]:
        """The node's own candidates as the faint numbered arrows the board
        already knows how to draw — the same picture the analysis panel
        shows, one level deeper."""
        items: list[tuple[int, int, str]] = []
        for pv_no in sorted(node.candidates):
            pv = node.candidates[pv_no].get("pv") or []
            if not pv:
                continue
            try:
                move = chess.Move.from_uci(pv[0])
            except ValueError:
                continue
            items.append((move.from_square, move.to_square, str(pv_no)))
        return items

    def _reset_detail(self) -> None:
        self._move_label.setText("—")
        self._chip.setText("…")
        self._chip.setProperty("tone", "even")
        theme.repolish(self._chip)
        self._depth_label.setText("")
        self._delta_label.setText("")
        self._sync_expand_btn(None)

    # ------------------------------------------------------------ branches
    def _on_branch_move(self, move: chess.Move) -> None:
        """A legal move was dragged on the dock board: grow it under the cursor."""
        if self._tree is None or self._cursor is None:
            return
        if not self._board.board.is_legal(move):
            return
        try:
            node = self._tree.add_child(self._cursor, move.uci(),
                                        source="user", note="你的分支")
        except ValueError:
            return
        self._canvas.sync()
        self._canvas.select_node(node)

    def expand_cursor(self) -> None:
        """Enter / Space / the top-bar button: the *toggle*, not just expand.

        On a node whose candidates are already out there is nothing left to
        expand, and the only thing the user can mean by pressing it again is
        「fold them back」 — so the same key does that. Without this, 展开候选
        was a one-way door: the only way back was to delete the whole tree.
        """
        node = self._cursor
        if node is None:
            return
        if graph_model.can_collapse(node):
            self.collapse_node(node)
        else:
            self.expand_node(node)

    def _spawn(self, tree: graph_model.Tree, node: graph_model.Node,
               pv_no: int, pv: list[str]) -> graph_model.Node | None:
        """Lay one engine candidate out as a lane — and remember it as one if
        this call is what created it.

        The ``existed`` test is what makes 收起候选 the exact inverse of
        展开候选. A node's first candidate is usually the move its lane
        already continues with (the PV continuation it was planted with), and
        ``add_child`` hands that *same* node back rather than a copy. Folding
        the candidates back must not truncate the line the user was reading,
        and a branch grown by hand that happens to be a candidate is the
        user's, not the engine's, so neither is recorded.

        Only the head of a chain is remembered; the rest of it is the
        variation's continuation and is not a candidate.
        """
        existed = tree.child(node, pv[0]) is not None
        chain = tree.grow_line(node, pv, source="engine",
                               note=f"引擎候选 #{pv_no}",
                               chain_note=f"引擎线 #{pv_no} 的延续")
        if not chain:
            return None
        head = chain[0]
        if not existed and head.id not in node.spawned:
            node.spawned.append(head.id)
        return head

    def expand_node(self, node: graph_model.Node) -> None:
        """Lay the node's candidates out as lanes (§5.2).

        Free in the common case: the MultiPV search that lit the node already
        returned its three continuations, so expanding is a redraw and not a
        search. Nodes come out unsearched — grey until you click them.
        """
        if not graph_model.can_expand(node):
            self._set_status("这个节点还没有候选可展开（先点它，让引擎给出候选）")
            return
        before = len(self._tree.nodes)
        for pv_no in sorted(node.candidates):
            pv = list(node.candidates[pv_no].get("pv") or [])[:graph_model.PV_LIMIT]
            if pv:
                self._spawn(self._tree, node, pv_no, pv)
        self._canvas.sync()
        self._show(node)
        self._set_status(
            f"铺开 {len(self._tree.nodes) - before} 个节点 · "
            "来自同一次搜索，未新增搜索 · 再按一次 Enter 可收起"
        )

    def collapse_node(self, node: graph_model.Node) -> None:
        """Fold the node's candidate lanes back in — an expand, undone.

        What the node *kept* is the point: the node itself, the rest of the
        lane it sits on, and any branch grown under it by hand all stay. Only
        the moves that came out of its own candidate search go.
        """
        if self._tree is None:
            return
        if not graph_model.can_collapse(node):
            self._set_status("这个节点没有已铺开的候选可收起")
            return
        removed = self._tree.collapse(node)
        ids = {n.id for n in removed}
        landed = self._forget(ids, fallback=node)
        self._canvas.sync()
        self._canvas.select_node(landed)
        self._set_status(
            f"收起 {len(ids)} 个候选节点 · 节点本身保留 · 需要时再展开，不重新搜索"
        )

    def kill_branch(self, node: graph_model.Node) -> None:
        """Kill a node, its own move and everything under it.

        The one irreversible action in this view, so it asks first whenever
        there is more to lose than the node itself. A leaf goes without a
        dialog: there is nothing behind it to lose, and a confirmation nobody
        reads is worse than none.
        """
        tree = self._tree
        if tree is None:
            return
        if node.parent is None:
            self._set_status("根节点不删——树锚定在它上面，删了整棵树就没有意义了")
            return
        doomed = tree.subtree(node)
        if len(doomed) > 1 and not self._confirm(
                "删除分支",
                f"删除 {graph_model.move_text(node)} 及其下 {len(doomed) - 1} 个节点？\n\n"
                "这一步没有撤销。"):
            self._set_status("已取消")
            return
        removed = tree.remove_subtree(node)
        ids = {n.id for n in removed}
        landed = self._forget(ids, fallback=node.parent)
        self._canvas.sync()
        self._canvas.select_node(landed)
        bits = [f"已删除 {graph_model.move_text(node)} 及其下 {len(ids) - 1} 个节点"]
        if graph_model.can_expand(node.parent):
            # Say it only when it is true: the ＋ coming back is the one piece
            # of good news here, and a kill that takes the last candidate lane
            # away is exactly when the user wants to know they can grow it
            # back for free (the eval cache still holds the position).
            bits.append("父节点已恢复可展开")
        bits.append("这一步没有撤销")
        self._set_status(" · ".join(bits))

    def _confirm(self, title: str, text: str) -> bool:
        """Ask before an irreversible delete.

        A method rather than a direct ``QMessageBox.question`` call so the
        smoke test can answer it without a modal dialog swallowing the run —
        and so the *policy* (when a delete asks at all) stays testable.
        """
        return QMessageBox.question(
            self, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def _forget(self, ids: set[str], fallback: graph_model.Node) -> graph_model.Node:
        """Drop removed nodes, and return the node the cursor should land on.

        Three things point at a node and all three have to be let go of
        together: an in-flight search (its result must not be written onto a
        node that is gone), the remembered last click, and the cursor — which
        can be sitting *inside* the branch that was just removed, in which
        case it falls back to the nearest surviving relative rather than to
        nothing.
        """
        if self._searching is not None and self._searching.id in ids:
            self._engine.cancel()
            self._searching = None
        if self._pending is not None and self._pending.id in ids:
            self._pending = None
        self._canvas.remove_nodes(ids)          # before anything walks _items
        cursor = self._cursor
        if cursor is None or cursor.id in ids:
            return fallback
        return cursor

    # -------------------------------------------------------------- search
    def _request(self, node: graph_model.Node) -> None:
        if node.searched or not node.searchable:
            return
        cached = (self._tree.eval_cache.get(graph_model.cache_key(node.fen))
                  if self._tree else None)
        if cached is not None:
            # A transposition: the position was searched under another path.
            self._apply_lines(node, cached["lines"], t=cached["t"])
            self._set_status(f"{graph_model.move_text(node)} · 缓存命中（该局面已搜过）")
            return
        if self._engine.busy:
            # Never drop the last click: cancel what is running and remember
            # this one; ``idle`` starts it as soon as the slot is really free.
            self._pending = node
            self._set_status(f"正在结束上一次搜索 · 之后搜索 {graph_model.move_text(node)}")
            self._engine.cancel()
            return
        self._start_search(node)

    def _start_search(self, node: graph_model.Node) -> None:
        try:
            self._engine.ensure_client()
        except EngineError as exc:
            self._set_status(f"引擎不可用：{exc}")
            return
        self._searching = node
        self._set_status(f"搜索 {graph_model.move_text(node)} · {SEARCH_MS}ms 浅搜…")

        def run(stop, info_sig):
            return self._engine.client.analyze(
                node.fen, movetime_ms=SEARCH_MS, multipv=MULTIPV,
                on_info=lambda info: info_sig.emit(info), stop=stop,
            )

        self._engine.submit(run, on_info=self._on_search_info,
                            on_done=self._on_search_done,
                            on_failed=self._on_search_failed)

    def _on_search_info(self, info: dict) -> None:
        node = self._searching
        if node is None or not self._active:
            return
        depth = info.get("depth")
        if depth:
            self._set_status(
                f"搜索 {graph_model.move_text(node)} · d{depth} · {SEARCH_MS}ms 浅搜…")

    def _on_search_done(self, result: dict) -> None:
        node, self._searching = self._searching, None
        if node is None:
            return
        lines = result.get("lines") or {}
        if lines and self._apply_lines(node, lines):
            bits = [f"节点 {graph_model.move_text(node)}"]
            if node.note:
                bits.append(node.note)
            bits.append(f"d{node.depth or '?'}")
            bits.append(f"{SEARCH_MS}ms 浅搜")
            self._set_status(" · ".join(bits))
        else:
            # No result — almost always because this search was superseded and
            # cancelled before the engine had produced a PV. The node stays
            # *unsearched* on purpose: marking it would block the retry that
            # the remembered last click is about to make, and would leave a
            # node the user did ask about permanently grey.
            self._set_status(f"{graph_model.move_text(node)} · 引擎没有返回评估（可再点一次）")

    def _on_search_failed(self, message: str) -> None:
        node, self._searching = self._searching, None
        self._set_status(f"搜索失败：{message}")
        if node is not None:
            node.searched_at = None                 # a later click may retry

    def _on_engine_idle(self) -> None:
        node, self._pending = self._pending, None
        if node is None or not self._active or not self.isVisible():
            return
        if not node.searched:
            self._request(node)

    def _apply_lines(self, node: graph_model.Node, lines: dict, t: float | None = None) -> bool:
        """Write a search result onto a node (and into the FEN cache)."""
        if not lines:
            return False
        best = lines.get(1) or next(iter(lines.values()))
        stm = graph_model.stm_of(node)
        node.eval_cp = graph_model.white_cp(best, stm)
        node.eval_mate = graph_model.white_mate(best, stm)
        node.depth = best.get("depth")
        node.searched_at = time.monotonic() if t is None else t
        node.candidates = {key: dict(value) for key, value in lines.items()}
        node.delta_cp = graph_model.delta_cp(node.parent, node)
        if self._tree is not None:
            self._tree.eval_cache[graph_model.cache_key(node.fen)] = {
                "lines": {key: dict(value) for key, value in lines.items()},
                "depth": node.depth,
                "t": node.searched_at,
            }
        item = self._canvas.item_for(node)
        if item is not None:
            item.update()
        if self._cursor is node:
            self._show(node)
        return True

    # --------------------------------------------------------------- nav
    def _nav_back(self) -> None:
        node = self._cursor
        if node is not None and node.parent is not None:
            self._canvas.select_node(node.parent)

    def _nav_forward(self) -> None:
        node = self._cursor
        if node is not None and node.children:
            self._canvas.select_node(node.children[0])

    def _nav_sibling(self, step: int) -> None:
        """↑/↓ — change lines. Without it a two-dimensional tree could only
        be crossed with the mouse, which is the one thing a keyboard-first
        step-through cannot do."""
        node = self._cursor
        if node is None or node.parent is None or self._tree is None:
            return
        siblings = self._tree.siblings(node)
        index = siblings.index(node) + step
        if 0 <= index < len(siblings):
            self._canvas.select_node(siblings[index])

    def _nav_home(self) -> None:
        if self._tree is not None:
            self._canvas.select_node(self._tree.root)

    # -------------------------------------------------------------- extras
    def copy_pgn(self) -> None:
        if self._tree is None or self._cursor is None:
            return
        path = self._tree.path_to(self._cursor)
        board = chess.Board(self._tree.anchor_fen)
        game = chess.pgn.Game()
        if board.board_fen() != chess.STARTING_BOARD_FEN:
            game.setup(board)
        node = game
        for step in path[1:]:
            if step.move is not None:
                node = node.add_variation(step.move)
        QApplication.clipboard().setText(str(game).strip())
        self._set_status(f"已复制根到光标的 {len(path) - 1} 手 PGN")

    def _on_board_seg(self) -> None:
        if self._active:
            self.exited.emit()

    def _toggle_dock(self) -> None:
        # ``isHidden`` and not ``isVisible``: this page is normally hidden as a
        # whole while the board page is up, and ``isVisible`` cannot tell that
        # apart from the dock having been collapsed.
        show = self._dock.isHidden()
        self._dock.setVisible(show)
        self._dock_btn.setText("收起棋盘" if show else "展开棋盘")

    def _on_zoom(self, factor: float) -> None:
        self._zoom_label.setText(f"{factor * 100:.0f}%")

    def _set_status(self, text: str) -> None:
        self._status.setText(text)
