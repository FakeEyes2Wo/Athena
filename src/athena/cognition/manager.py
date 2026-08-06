"""CognitionManager — Agent cognitive state machine.

Plan tree CRUD, Checkpoint/Track/Reflect/Guard, filesystem persistence,
Mermaid visualisation, and smart hint mechanism.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from athena.cognition.mermaid import render_mermaid
from athena.cognition.schemas import (
    Checkpoint,
    GuardResult,
    GuardRule,
    Plan,
    PlanNode,
    PlanNodeRelation,
    PlanNodeStatus,
    Reflection,
    ToolCategory,
    TrackState,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uid(prefix: str = "") -> str:
    """Generate a short unique ID with optional prefix."""
    short = uuid.uuid4().hex[:8]
    return f"{prefix}{short}" if prefix else short


def _dict_to_node(d: dict, parent_id: str) -> PlanNode:
    """Convert an LLM-supplied step dict to a PlanNode.

    Recursively builds children with correct parent_id references.
    """
    node_id = _uid(f"{parent_id}#")
    children = [
        _dict_to_node(c, parent_id=node_id)
        for c in d.get("children", [])
    ]
    title = str(d.get("title", ""))
    return PlanNode(
        id=node_id,
        parent_id=parent_id,
        title=title[:80],
        description=str(d.get("description", "")),
        relation=PlanNodeRelation(d.get("relation", "sequential")),
        children=children,
    )


class CognitionManager:
    """Agent cognitive state machine.

    Manages Plan tree, Checkpoints, Reflections, Guard rules, and Track state
    in memory. Persists to ``{work_root}/cognition/`` on every state change.
    """

    def __init__(self, state_store=None, work_root: str = "work") -> None:
        self._plan: Plan | None = None
        self._checkpoints: list[Checkpoint] = []
        self._reflections: list[Reflection] = []
        self._guard_rules: list[GuardRule] = []
        self._work_root = Path(work_root)
        self._cog_dir = self._work_root / "cognition"
        self._tool_call_window: list[str] = []
        self._plan_called = False

    # Plan tree operations

    async def create_plan(self, goal: str, root_nodes: list[dict]) -> Plan:
        """Create a new Plan tree from LLM-supplied step descriptions.

        Auto-sets root and first child to IN_PROGRESS.

        Args:
            goal: Top-level goal statement.
            root_nodes: List of dicts with 'title', 'description', 'relation',
                and optional 'children' (recursive).

        Returns:
            The newly created Plan.
        """
        plan_id = _uid("plan-")
        children = [_dict_to_node(n, parent_id=plan_id) for n in root_nodes]
        root = PlanNode(
            id=plan_id,
            title=goal,
            description="Top-level goal",
            relation=PlanNodeRelation.SEQUENTIAL,
            children=children,
        )
        plan = Plan(
            plan_id=plan_id,
            goal=goal,
            root=root,
            created_at=_now(),
            updated_at=_now(),
        )
        self._plan = plan
        self._plan_called = True

        # auto-set root and first child to IN_PROGRESS
        self._plan.root.status = PlanNodeStatus.IN_PROGRESS
        self._plan.root.updated_at = _now()
        if children:
            children[0].status = PlanNodeStatus.IN_PROGRESS
            children[0].updated_at = _now()

        self._write_mermaid_file()
        self._write_plan_json()
        return plan

    async def get_plan(self) -> Plan | None:
        """Return the current Plan or None if not yet created."""
        return self._plan

    async def update_node_status(
        self, node_id: str, status: str, evidence: str = ""
    ) -> PlanNode:
        """Update a PlanNode's status and evidence, auto-advance to next pending.

        Args:
            node_id: The node to update.
            status: One of 'completed', 'failed', 'skipped'.
            evidence: Summary of what was done/discovered.

        Returns:
            The updated PlanNode.

        Raises:
            ValueError: If node_id not found in the current plan tree.
        """
        if self._plan is None:
            raise ValueError("No plan exists. Call create_plan first.")
        node = self._find_node(node_id)
        if node is None:
            existing = self._all_node_ids()
            raise ValueError(
                f"Node '{node_id}' not found. Existing nodes: {existing}"
            )
        node.status = PlanNodeStatus(status)
        node.evidence = evidence
        node.updated_at = _now()
        self._plan.updated_at = _now()
        if status == "completed":
            self._auto_advance(node)
        self._write_mermaid_file()
        self._write_plan_json()
        return node

    async def add_child_nodes(
        self, parent_id: str, nodes: list[dict]
    ) -> list[PlanNode]:
        """Add child nodes under an existing PlanNode.

        Args:
            parent_id: The parent node's ID.
            nodes: List of dicts (same format as create_plan root_nodes).

        Returns:
            The newly created PlanNode list.

        Raises:
            ValueError: If no plan exists or parent node not found.
        """
        if self._plan is None:
            raise ValueError("No plan exists.")
        parent = self._find_node(parent_id)
        if parent is None:
            raise ValueError(f"Parent node '{parent_id}' not found.")
        new_nodes = [_dict_to_node(n, parent_id=parent_id) for n in nodes]
        parent.children.extend(new_nodes)
        self._plan.updated_at = _now()
        self._write_mermaid_file()
        self._write_plan_json()
        return new_nodes

    async def add_alternative_branch(
        self, parent_id: str, branch_title: str, branch_desc: str = ""
    ) -> PlanNode:
        """Add an alternative approach branch under a parent node.

        Sets the parent's relation to ALTERNATIVE if not already.

        Args:
            parent_id: The parent node's ID.
            branch_title: Title for the alternative branch.
            branch_desc: Optional description of the alternative approach.

        Returns:
            The newly created alternative branch PlanNode.

        Raises:
            ValueError: If no plan exists or parent node not found.
        """
        if self._plan is None:
            raise ValueError("No plan exists.")
        parent = self._find_node(parent_id)
        if parent is None:
            raise ValueError(f"Parent node '{parent_id}' not found.")
        node = PlanNode(
            id=_uid(f"{parent_id}#alt-"),
            parent_id=parent_id,
            title=branch_title,
            description=branch_desc,
        )
        parent.children.append(node)
        parent.relation = PlanNodeRelation.ALTERNATIVE
        self._plan.updated_at = _now()
        self._write_mermaid_file()
        self._write_plan_json()
        return node

    # Checkpoint operations

    async def create_checkpoint(
        self,
        node_id: str,
        summary: str,
        artifact_refs: list[str] | None = None,
    ) -> Checkpoint:
        """Create a new Checkpoint tied to a PlanNode.

        Args:
            node_id: The PlanNode ID this checkpoint relates to.
            summary: What was accomplished and discovered.
            artifact_refs: Optional list of artifact references.

        Returns:
            The newly created Checkpoint.

        Raises:
            ValueError: If no plan exists.
        """
        if self._plan is None:
            raise ValueError("No plan exists.")
        cp = Checkpoint(
            checkpoint_id=_uid("cp-"),
            plan_id=self._plan.plan_id,
            node_id=node_id,
            summary=summary,
            artifact_refs=artifact_refs or [],
        )
        self._checkpoints.append(cp)
        node = self._find_node(node_id)
        if node is not None:
            node.checkpoint_ref = cp.checkpoint_id
        self._write_checkpoints()
        return cp

    async def list_checkpoints(self) -> list[Checkpoint]:
        """Return all checkpoints in creation order."""
        return list(self._checkpoints)

    async def get_last_checkpoint(self) -> Checkpoint | None:
        """Return the most recent checkpoint or None."""
        return self._checkpoints[-1] if self._checkpoints else None

    # Track operations

    async def get_current_position(self) -> TrackState:
        """Build a TrackState snapshot of current progress.

        Returns:
            TrackState with current node, counts, and path to root.
        """
        if self._plan is None:
            return TrackState(plan_id="")
        all_nodes = self._all_nodes_list()
        active = [n.id for n in all_nodes if n.status == PlanNodeStatus.IN_PROGRESS]
        completed = sum(1 for n in all_nodes if n.status == PlanNodeStatus.COMPLETED)
        failed = sum(1 for n in all_nodes if n.status == PlanNodeStatus.FAILED)
        pending = sum(1 for n in all_nodes if n.status == PlanNodeStatus.PENDING)
        current_id = active[0] if active else None
        path = self._path_to_root(current_id) if current_id else []
        return TrackState(
            plan_id=self._plan.plan_id,
            current_node_id=current_id,
            active_nodes=active,
            completed_count=completed,
            failed_count=failed,
            pending_count=pending,
            path_to_root=path,
        )

    async def get_next_pending(self) -> list[PlanNode]:
        """Return the first layer of pending nodes (BFS from root).

        Only descends into IN_PROGRESS nodes to find their pending children.
        """
        if self._plan is None:
            return []
        result: list[PlanNode] = []
        queue: list[PlanNode] = [self._plan.root]
        while queue:
            node = queue.pop(0)
            if node.status == PlanNodeStatus.PENDING:
                result.append(node)
            elif node.status == PlanNodeStatus.IN_PROGRESS:
                queue.extend(node.children)
        return result

    # Reflect operations

    async def create_reflection(
        self,
        trigger: str,
        observations: str,
        adjustment: str = "",
        plan_changes: list[dict] | None = None,
    ) -> Reflection:
        """Record a reflective review entry.

        Args:
            trigger: Why this reflection was triggered.
            observations: What actually happened.
            adjustment: Proposed plan adjustment.
            plan_changes: Concrete tree structure changes made.

        Returns:
            The newly created Reflection.

        Raises:
            ValueError: If no plan exists.
        """
        if self._plan is None:
            raise ValueError("No plan exists.")
        r = Reflection(
            reflection_id=_uid("refl-"),
            plan_id=self._plan.plan_id,
            trigger=trigger,
            observations=observations,
            adjustment=adjustment,
            plan_changes=plan_changes or [],
        )
        self._reflections.append(r)
        self._write_reflections()
        return r

    # Guard operations

    async def register_guard_rules(self, rules: list[GuardRule]) -> None:
        """Register hard constraint rules, replacing any previous rules."""
        self._guard_rules = list(rules)
        self._write_guard_rules()

    async def check_action(self, action_desc: str) -> GuardResult:
        """Check if an action description matches any guard rule patterns.

        Simple substring match — pattern appears anywhere in action_desc.
        Violations (severity="error") set allowed=False; warnings do not block.

        Args:
            action_desc: The action description to check.

        Returns:
            GuardResult with allowed flag, violations list, and warnings list.
        """
        violations: list[str] = []
        warnings: list[str] = []
        action_lower = action_desc.lower()
        for rule in self._guard_rules:
            if rule.pattern.lower() in action_lower:
                if rule.severity == "error":
                    violations.append(
                        f"{rule.description} (rule: {rule.rule_id})"
                    )
                else:
                    warnings.append(
                        f"{rule.description} (rule: {rule.rule_id})"
                    )
        return GuardResult(
            allowed=len(violations) == 0,
            violations=violations,
            warnings=warnings,
        )

    # Hint mechanism

    def record_tool_call(self, tool_name: str) -> None:
        """Record a tool call for category migration detection."""
        self._tool_call_window.append(tool_name)
        if len(self._tool_call_window) > 3:
            self._tool_call_window = self._tool_call_window[-3:]

    def check_plan_hint(self) -> str | None:
        """Return a hint if agent_plan has never been called."""
        if not self._plan_called:
            return (
                "Hint: 建议先调用 agent_plan 制定执行计划，"
                "有助于跟踪长工作流的进度。"
            )
        return None

    def check_migration_hint(
        self, tool_name: str, get_category
    ) -> str | None:
        """Return a hint if the tool category has migrated vs recent calls.

        Args:
            tool_name: The name of the tool just called.
            get_category: A callable that maps tool_name to ToolCategory.

        Returns:
            A hint string if a category migration is detected, or None.
        """
        if len(self._tool_call_window) < 2:
            return None
        current_cat = get_category(tool_name)
        if current_cat in (ToolCategory.COGNITION, None):
            return None
        prev_cats = {
            get_category(n)
            for n in self._tool_call_window[:-1]
            if get_category(n) not in (ToolCategory.COGNITION, None)
        }
        if len(prev_cats) == 1 and current_cat not in prev_cats:
            cat_hints = {
                ToolCategory.SEARCH: "检索阶段",
                ToolCategory.DOWNLOAD: "下载阶段",
                ToolCategory.INSPECT: "探查阶段",
                ToolCategory.COMPUTE: "计算阶段",
                ToolCategory.ARTIFACT: "产出阶段",
            }
            prev_label = cat_hints.get(
                next(iter(prev_cats)), "上一阶段"
            )
            return (
                f"Hint: {prev_label}似已完成。如果结论已明确，"
                "建议调用 agent_checkpoint 标记当前步骤为 completed。"
            )
        return None

    def check_track_after_track_hint(
        self, tool_name: str
    ) -> str | None:
        """If agent_track is called, suggest checkpoint for active nodes.

        Args:
            tool_name: The name of the tool just called.

        Returns:
            A hint string if there are active nodes needing completion, or None.
        """
        if tool_name != "agent_track" or self._plan is None:
            return None
        all_nodes = self._all_nodes_list()
        active = [
            n for n in all_nodes if n.status == PlanNodeStatus.IN_PROGRESS
        ]
        if not active:
            return None
        node = active[0]
        return (
            f"当前步骤 '{node.id}' ({node.title}) 仍为 in_progress。"
            "如需标记完成，请调用 "
            f"agent_checkpoint(node_id='{node.id}', status='completed', ...)"
        )

    # Persistence

    async def save(self) -> str:
        """Persist all state to work_root and return the plan artifact ref.

        Returns:
            The path to plan.json as a string.
        """
        self._cog_dir.mkdir(parents=True, exist_ok=True)
        self._write_plan_json()
        self._write_mermaid_file()
        self._write_checkpoints()
        self._write_reflections()
        self._write_guard_rules()
        return str(self._cog_dir / "plan.json")

    async def restore(self, artifact_ref: str) -> None:
        """Restore Plan tree and state from a previously saved plan.json.

        Also restores checkpoints from checkpoints/ directory and guard rules
        from guard_rules.json if present.

        Args:
            artifact_ref: Path to the plan.json file.

        Raises:
            FileNotFoundError: If the plan file does not exist.
        """
        plan_path = Path(artifact_ref)
        if not plan_path.exists():
            raise FileNotFoundError(f"Plan file not found: {artifact_ref}")
        raw = json.loads(plan_path.read_text(encoding="utf-8"))
        self._plan = Plan.model_validate(raw)
        self._plan_called = True
        # restore checkpoints if present
        cps_dir = self._cog_dir / "checkpoints"
        if cps_dir.exists():
            self._checkpoints = []
            for f in sorted(cps_dir.glob("*.json")):
                cp_raw = json.loads(f.read_text(encoding="utf-8"))
                self._checkpoints.append(Checkpoint.model_validate(cp_raw))
        # restore guard rules if present
        gr_path = self._cog_dir / "guard_rules.json"
        if gr_path.exists():
            gr_raw = json.loads(gr_path.read_text(encoding="utf-8"))
            self._guard_rules = [
                GuardRule.model_validate(r)
                for r in (gr_raw if isinstance(gr_raw, list) else [])
            ]

    # Mermaid output

    def _render_mermaid(self) -> str:
        """Generate Mermaid string from current plan."""
        if self._plan is None:
            return "graph TD\n    empty(\"No plan yet\")\n"
        return render_mermaid(self._plan)

    def _write_mermaid_file(self) -> None:
        """Write plan.mermaid to cog_dir, overwriting previous."""
        self._cog_dir.mkdir(parents=True, exist_ok=True)
        mermaid_str = self._render_mermaid()
        (self._cog_dir / "plan.mermaid").write_text(
            mermaid_str, encoding="utf-8"
        )

    # Internal helpers

    def _find_node(self, node_id: str) -> PlanNode | None:
        """Find a node by ID in the current plan tree."""
        if self._plan is None:
            return None
        return self._find_in_children(node_id, self._plan.root)

    def _find_in_children(
        self, node_id: str, node: PlanNode
    ) -> PlanNode | None:
        if node.id == node_id:
            return node
        for child in node.children:
            found = self._find_in_children(node_id, child)
            if found is not None:
                return found
        return None

    def _all_nodes_list(self) -> list[PlanNode]:
        """Return all nodes in the plan tree (flat list)."""
        if self._plan is None:
            return []
        return self._collect_nodes(self._plan.root)

    def _all_node_ids(self) -> list[str]:
        return [n.id for n in self._all_nodes_list()]

    def _collect_nodes(self, node: PlanNode) -> list[PlanNode]:
        """DFS collection of all nodes in subtree."""
        result = [node]
        for child in node.children:
            result.extend(self._collect_nodes(child))
        return result

    def _auto_advance(self, completed_node: PlanNode) -> None:
        """After a node completes, set the first pending child or sibling
        to in_progress."""
        # try children first
        for child in completed_node.children:
            if child.status == PlanNodeStatus.PENDING:
                child.status = PlanNodeStatus.IN_PROGRESS
                child.updated_at = _now()
                return
        # try next sibling
        parent = (
            self._find_node(completed_node.parent_id)
            if completed_node.parent_id
            else None
        )
        if parent is not None:
            found_completed = False
            for sibling in parent.children:
                if found_completed and sibling.status == PlanNodeStatus.PENDING:
                    sibling.status = PlanNodeStatus.IN_PROGRESS
                    sibling.updated_at = _now()
                    return
                if sibling.id == completed_node.id:
                    found_completed = True

    def _path_to_root(self, node_id: str) -> list[str]:
        """Build the path of node IDs from node_id up to root."""
        path: list[str] = []
        current_id: str | None = node_id
        while current_id is not None:
            path.append(current_id)
            node = self._find_node(current_id)
            current_id = node.parent_id if node else None
        return path

    def _write_plan_json(self) -> None:
        """Write plan.json to cog_dir."""
        if self._plan is None:
            return
        self._cog_dir.mkdir(parents=True, exist_ok=True)
        (self._cog_dir / "plan.json").write_text(
            self._plan.model_dump_json(indent=2), encoding="utf-8"
        )

    def _write_checkpoints(self) -> None:
        """Write all checkpoints to individual timestamped JSON files."""
        cps_dir = self._cog_dir / "checkpoints"
        cps_dir.mkdir(parents=True, exist_ok=True)
        for cp in self._checkpoints:
            (cps_dir / f"{cp.checkpoint_id}.json").write_text(
                cp.model_dump_json(indent=2), encoding="utf-8"
            )

    def _write_reflections(self) -> None:
        """Write all reflections to individual timestamped JSON files."""
        refl_dir = self._cog_dir / "reflections"
        refl_dir.mkdir(parents=True, exist_ok=True)
        for r in self._reflections:
            (refl_dir / f"{r.reflection_id}.json").write_text(
                r.model_dump_json(indent=2), encoding="utf-8"
            )

    def _write_guard_rules(self) -> None:
        """Write guard rules to guard_rules.json."""
        self._cog_dir.mkdir(parents=True, exist_ok=True)
        rules_json = json.dumps(
            [r.model_dump() for r in self._guard_rules],
            ensure_ascii=False,
            indent=2,
        )
        (self._cog_dir / "guard_rules.json").write_text(
            rules_json, encoding="utf-8"
        )
