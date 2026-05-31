"""Typer entry point. Subcommands are thin wrappers around module functions."""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import (
    chain as chain_mod,
    collector,
    config,
    init as init_mod,
    knowledge_graph as kg_mod,
    memory as memory_mod,
    merger,
    orchestrator,
    project as project_mod,
    reviewer,
    skill_importer,
    skill_registry,
    skill_router,
    skill_source,
    team as team_mod,
    team_orchestrator,
)
from .models import ProjectConfig

app = typer.Typer(
    name="johnstudio",
    help="Local-first AI dev-team orchestrator.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.callback()
def _root() -> None:
    """Root callback."""


@app.command("server")
def cmd_server(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Run the local FastAPI server that backs the desktop UI."""
    from . import server as server_mod
    console.print(f"[green]Starting JohnStudio API on[/green] http://{host}:{port}")
    server_mod.serve(host=host, port=port, reload=reload)


@app.command("init")
def cmd_init() -> None:
    """Initialize the global JohnStudio home, DB, memory, and graph folders."""
    status = init_mod.run_init()
    console.print(f"[green]JohnStudio initialized at[/green] {status['home']}")
    t = Table("tool", "available")
    for k, v in status["tools_detected"].items():
        t.add_row(k, "yes" if v else "no")
    console.print(t)
    console.print(
        f"FTS5: {'yes' if status['fts5'] else 'no (LIKE fallback)'}  "
        f"DB: {status['db_path']}"
    )


@app.command("research")
def cmd_research(
    output: Path = typer.Option(
        None,
        "--output",
        "-o",
        help="Destination file. Default: ./docs/research/repo_research_report.md",
    ),
) -> None:
    """Copy the baked-in repo research report into the current project."""
    dst = init_mod.run_research(output)
    console.print(f"[green]Wrote research report:[/green] {dst}")


@app.command("add-project")
def cmd_add_project(name: str, repo_path: Path) -> None:
    """Register a git repo as a JohnStudio project."""
    try:
        status = project_mod.add_project(name, repo_path)
    except project_mod.NotAGitRepoError as e:
        console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=2)
    except FileNotFoundError as e:
        console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=2)
    console.print(
        f"[green]Registered[/green] project [bold]{name}[/bold] "
        f"(id={status['project_id']}, base={status['base_branch']})"
    )
    stack = status["stack"]
    if stack["detected_files"]:
        console.print(f"  stack: {', '.join(stack['languages'] + stack['frameworks'])}")
        console.print(f"  markers: {', '.join(stack['detected_files'])}")


@app.command("projects")
def cmd_projects() -> None:
    """List registered projects."""
    rows = project_mod.list_projects()
    if not rows:
        console.print("[dim]No projects registered yet.[/dim]")
        return
    t = Table("id", "name", "repo_path", "base")
    for r in rows:
        t.add_row(str(r["id"]), r["name"], r["repo_path"], r["base_branch"])
    console.print(t)


# ---------------------------------------------------------------------------
# skill ...
# ---------------------------------------------------------------------------

skill_app = typer.Typer(no_args_is_help=True, help="Skill registry, sources, routing.")
source_app = typer.Typer(no_args_is_help=True, help="Manage skill sources.")
skill_app.add_typer(source_app, name="source")
app.add_typer(skill_app, name="skill")


@source_app.command("add")
def cmd_source_add(repo_or_path: str) -> None:
    """Register a skill source (local path or remote URL)."""
    s = skill_source.add_source(repo_or_path)
    console.print(f"[green]Source registered[/green] id={s['id']} (local={s['local']})")


@source_app.command("scan")
def cmd_source_scan() -> None:
    """Import all local sources into the registry."""
    for r in skill_source.scan_sources():
        console.print(r)


@skill_app.command("import")
def cmd_skill_import(source: Path) -> None:
    """Import skills directly from a local directory (no source registration needed)."""
    imported = skill_importer.import_dir(source, source_repo=str(source))
    console.print(f"[green]Imported {len(imported)} skills[/green] from {source}")


@skill_app.command("import-seeds")
def cmd_skill_import_seeds() -> None:
    """Import the bundled seed skills."""
    imported = skill_importer.import_seeds()
    console.print(f"[green]Imported {len(imported)} seed skills[/green]")


@skill_app.command("list")
def cmd_skill_list(enabled: bool = typer.Option(False, "--enabled-only"), category: str = typer.Option(None)) -> None:
    rows = skill_registry.list_skills(enabled_only=enabled, category=category)
    if not rows:
        console.print("[dim]No skills.[/dim]")
        return
    t = Table("id", "type", "category", "enabled", "trust", "name")
    for r in rows:
        t.add_row(
            r["skill_id"], r["type"], r["category"] or "?",
            "yes" if r["enabled"] else "no", r["trust_level"], r["name"]
        )
    console.print(t)


@skill_app.command("show")
def cmd_skill_show(skill_id: str) -> None:
    d = skill_registry.show_skill(skill_id)
    if not d:
        console.print(f"[red]Not found:[/red] {skill_id}")
        raise typer.Exit(2)
    console.print(d)


@skill_app.command("search")
def cmd_skill_search(query: str) -> None:
    for r in skill_registry.search_skills(query):
        console.print(f"  [bold]{r['skill_id']}[/bold] [{r['category']}] — {r['description']}")


@skill_app.command("enable")
def cmd_skill_enable(skill_id: str) -> None:
    skill_registry.set_enabled(skill_id, True)
    console.print(f"[green]Enabled[/green] {skill_id}")


@skill_app.command("disable")
def cmd_skill_disable(skill_id: str) -> None:
    skill_registry.set_enabled(skill_id, False)
    console.print(f"[yellow]Disabled[/yellow] {skill_id}")


@skill_app.command("pin")
def cmd_skill_pin(project: str, skill_id: str) -> None:
    p = project_mod.get_project(project)
    if not p:
        console.print(f"[red]Project not found:[/red] {project}")
        raise typer.Exit(2)
    pinned = skill_registry.pin_skill(p["repo_path"], skill_id)
    console.print(f"Pinned for {project}: {pinned}")


@skill_app.command("unpin")
def cmd_skill_unpin(project: str, skill_id: str) -> None:
    p = project_mod.get_project(project)
    if not p:
        console.print(f"[red]Project not found:[/red] {project}")
        raise typer.Exit(2)
    pinned = skill_registry.unpin_skill(p["repo_path"], skill_id)
    console.print(f"Pinned for {project}: {pinned}")


@skill_app.command("route")
def cmd_skill_route(
    project: str,
    task: str,
    role: str = typer.Option("backend_implementer", "--role"),
) -> None:
    """Show the skills the router would select for a given (project, task, role)."""
    pinfo = project_mod.get_project(project)
    if not pinfo:
        console.print(f"[red]Project not found:[/red] {project}")
        raise typer.Exit(2)
    pcfg: ProjectConfig = config.load_project_config(pinfo["repo_path"])
    req = skill_router.RouteRequest(
        project=pcfg,
        agent_role=role,
        task_text=task,
        relevant_files=[],
        feedback=skill_router.previous_feedback(),
    )
    selected = skill_router.route(req)
    if not selected:
        console.print("[dim]No skills selected (try importing seeds first).[/dim]")
        return
    t = Table("score", "tokens", "skill", "rationale")
    for s in selected:
        t.add_row(f"{s.score:.0f}", str(s.tokens), s.skill_id, s.rationale)
    console.print(t)


# ---------------------------------------------------------------------------
# run / status / stop / cleanup / resume
# ---------------------------------------------------------------------------

@app.command("run")
def cmd_run(
    project: str,
    task: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    stub_only: bool = typer.Option(False, "--stub-only"),
    workers_list: str = typer.Option(None, "--workers", help="Comma-separated worker names"),
    max_agents: int = typer.Option(None, "--max-agents"),
) -> None:
    """Start a coordinated task."""
    req = [w.strip() for w in workers_list.split(",")] if workers_list else None
    try:
        r = orchestrator.run(
            project, task, dry_run=dry_run, stub_only=stub_only,
            requested_workers=req, max_agents=max_agents,
        )
    except KeyError as e:
        console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(2)
    console.print(
        f"[green]Task {r['task_number']:04d} {'(dry-run) ' if r['dry_run'] else ''}{'launched' if not r['dry_run'] else 'planned'}[/green]"
    )
    console.print(f"  folder: {r['task_folder']}")
    console.print(f"  team: {', '.join(r['team'])}")
    if r.get("session"):
        console.print(f"  tmux session: {r['session']}  (attach: `tmux attach -t {r['session']}`)")


@app.command("status")
def cmd_status(project: str, task_number: int) -> None:
    s = orchestrator.status(task_number, project)
    console.print(f"[bold]Task {s['task_number']:04d}[/bold]  status={s['status']}  — {s['title']}")
    t = Table("worker", "status", "branch", "RESULT.md", "DONE.md")
    for r in s["runs"]:
        t.add_row(
            r["worker"], r["status"], r["branch"] or "-",
            "yes" if r["result_md_exists"] else "no",
            "yes" if r["done_md_exists"] else "no",
        )
    console.print(t)


@app.command("stop")
def cmd_stop(project: str, task_number: int) -> None:
    r = orchestrator.stop(task_number, project)
    console.print(f"Stopped task {r['task_number']:04d}, session={r['session']}")


@app.command("cleanup")
def cmd_cleanup(
    project: str, task_number: int,
    prune_worktrees: bool = typer.Option(False, "--prune-worktrees"),
) -> None:
    r = orchestrator.cleanup(task_number, project, prune_worktrees=prune_worktrees)
    console.print(f"Cleanup task {r['task_number']:04d}; removed worktrees: {r['removed_worktrees']}")


@app.command("resume")
def cmd_resume(project: str, task_number: int, worker: str) -> None:
    r = orchestrator.resume(task_number, project, worker)
    if r["resumed"]:
        console.print(f"[green]Re-nudged[/green] {worker} in tmux pane {r['pane']}")
    else:
        console.print(f"Prompt rewritten at {r['prompt']} (no live session to re-nudge)")


# ---------------------------------------------------------------------------
# collect / review / merge
# ---------------------------------------------------------------------------

@app.command("collect")
def cmd_collect(project: str, task_number: int) -> None:
    s = collector.collect(task_number, project)
    console.print(f"[green]Collected task {task_number:04d}[/green]  workers={len(s['runs'])}")
    for r in s["runs"]:
        flags = []
        if r["protected_path_hits"]:
            flags.append(f"PROTECTED:{r['protected_path_hits']}")
        if r["dangerous_command_hits"]:
            flags.append(f"DANGEROUS:{r['dangerous_command_hits']}")
        if r["approval_command_hits"]:
            flags.append(f"APPROVAL:{r['approval_command_hits']}")
        console.print(
            f"  {r['worker']}  files={len(r['files_changed'])} "
            f"tests={[t['exit_code'] for t in r['tests']]}  flags={flags}"
        )


@app.command("review")
def cmd_review(project: str, task_number: int) -> None:
    r = reviewer.review(task_number, project)
    console.print(f"[green]Review written[/green] → {r['final_review_path']}")
    console.print(f"Merge plan → {r['merge_plan_path']}")
    t = Table("worker", "score", "flags")
    for s in r["scores"]:
        t.add_row(s["worker_name"], str(s["score"]), ",".join(s["flags"]) or "—")
    console.print(t)
    if r["recommended"]:
        console.print(f"[bold]Recommended:[/bold] [cyan]{r['recommended']}[/cyan]")


@app.command("merge")
def cmd_merge(
    project: str,
    task_number: int,
    worker_name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes", help="Tests-only short-circuit"),
) -> None:
    if dry_run:
        out = merger.merge(task_number, project, worker_name, dry_run=True)
        console.print(out)
        return
    if not yes:
        confirm = typer.confirm(
            f"Merge worker `{worker_name}` for task {task_number:04d} into base?",
            default=False,
        )
        if not confirm:
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(1)
    try:
        out = merger.merge(task_number, project, worker_name, confirm=True)
    except merger.MergeAborted as e:
        console.print(f"[red]Aborted:[/red] {e}")
        raise typer.Exit(2)
    if out.get("merged"):
        console.print(f"[green]Merged[/green] {out['branch']}  tests_passed={out.get('tests_passed')}")
    else:
        console.print(f"[red]Merge failed:[/red] exit={out.get('exit_code')}")


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------

memory_app = typer.Typer(no_args_is_help=True, help="Memory vault & knowledge graph commands.")
app.add_typer(memory_app, name="memory")


@memory_app.command("open")
def cmd_memory_open(project: str) -> None:
    p = project_mod.get_project(project)
    if not p:
        raise typer.Exit(2)
    path = memory_mod.memory_root(p["repo_path"])
    console.print(f"Memory vault: {path}")


@memory_app.command("graph")
def cmd_memory_graph(project: str) -> None:
    p = project_mod.get_project(project)
    if not p:
        raise typer.Exit(2)
    entities = kg_mod.list_entities(p["id"])
    rels = kg_mod.list_relationships(p["id"])
    console.print(f"Entities: {len(entities)}  Relationships: {len(rels)}")
    t = Table("type", "name", "path")
    for e in entities:
        t.add_row(e["entity_type"], e["name"], e["path"] or "-")
    console.print(t)


@memory_app.command("entities")
def cmd_memory_entities(project: str) -> None:
    p = project_mod.get_project(project)
    if not p:
        raise typer.Exit(2)
    for e in kg_mod.list_entities(p["id"]):
        console.print(f"- [{e['entity_type']}] {e['name']}")


@memory_app.command("backlinks")
def cmd_memory_backlinks(project: str, note: str) -> None:
    p = project_mod.get_project(project)
    if not p:
        raise typer.Exit(2)
    idx = kg_mod.build_backlink_index(p["repo_path"])
    for src in idx.get(note, []):
        console.print(f"- {src}")


@memory_app.command("relate")
def cmd_memory_relate(project: str, note_a: str, note_b: str, relation: str = "related") -> None:
    p = project_mod.get_project(project)
    if not p:
        raise typer.Exit(2)
    # The CLI accepts entity names; require pre-existing entities.
    rows = kg_mod.list_entities(p["id"])
    types = {r["name"]: r["entity_type"] for r in rows}
    if note_a not in types or note_b not in types:
        console.print("[red]Both notes must exist as entities first.[/red]")
        raise typer.Exit(2)
    kg_mod.link_entities(
        p["id"],
        (types[note_a], note_a),
        (types[note_b], note_b),
        relation,
    )
    console.print(f"Linked: {note_a} -[{relation}]-> {note_b}")


@memory_app.command("tag")
def cmd_memory_tag(project: str) -> None:
    p = project_mod.get_project(project)
    if not p:
        raise typer.Exit(2)
    root = memory_mod.memory_root(p["repo_path"])
    from .utils import iter_markdown_files
    for f in iter_markdown_files(root):
        added = kg_mod.auto_tag_note(f)
        if added:
            console.print(f"+{added}  {f}")


# ---------------------------------------------------------------------------
# chain (RFC → implement → review → revise → merge)
# ---------------------------------------------------------------------------

chain_app = typer.Typer(no_args_is_help=True, help="RFC-driven chain mode.")
app.add_typer(chain_app, name="chain")


@chain_app.command("run")
def cmd_chain_run(
    project: str,
    task: str,
    architect: str = typer.Option("claude_review", "--architect"),
    rfc_reviewer: str = typer.Option("claude_review", "--rfc-reviewer"),
    implementer: str = typer.Option("claude_backend", "--implementer"),
    reviewer_w: str = typer.Option("claude_review", "--reviewer"),
) -> None:
    """Start a chain task. Launches the RFC phase; chain pauses at the RFC gate."""
    try:
        out = chain_mod.begin_chain(
            project_name=project, task_text=task,
            architect_worker=architect, rfc_reviewer_worker=rfc_reviewer,
            implementer_worker=implementer, reviewer_worker=reviewer_w,
        )
    except KeyError as e:
        console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(2)
    console.print(
        f"[green]Chain task {out['task_number']:04d} started.[/green]  "
        f"First phase: {out['first_phase']}"
    )
    # Immediately launch the first phase.
    launch = chain_mod.run_phase(out["task_db_id"])
    console.print(f"  launched {launch.get('phase')} round={launch.get('round')} worker={launch.get('worker')}")
    console.print(f"  prompt: {launch.get('prompt_path')}")
    console.print("Use `johnstudio chain advance <task_n>` to poll for completion and step the state machine.")


@chain_app.command("advance")
def cmd_chain_advance(project: str, task_number: int) -> None:
    """Poll the current phase: if its artifacts exist, finalize and start the next phase."""
    pinfo = project_mod.get_project(project)
    if not pinfo:
        console.print(f"[red]project not found:[/red] {project}")
        raise typer.Exit(2)
    task_db_id = _task_db_id(pinfo["id"], task_number)
    out = chain_mod.complete_current_phase_if_ready(task_db_id)
    console.print(out)
    # If we advanced to a non-human, non-terminal phase, kick it off.
    cur = chain_mod.current_phase(task_db_id)
    if cur and cur.phase not in chain_mod.HUMAN_GATES and cur.phase not in chain_mod.TERMINAL and cur.status == "pending":
        launch = chain_mod.run_phase(task_db_id)
        console.print(f"  → launched {launch.get('phase')} round={launch.get('round')}")


@chain_app.command("status")
def cmd_chain_status(project: str, task_number: int) -> None:
    pinfo = project_mod.get_project(project)
    if not pinfo:
        raise typer.Exit(2)
    task_db_id = _task_db_id(pinfo["id"], task_number)
    phases = chain_mod.list_phases(task_db_id)
    t = Table("#", "phase", "round", "status", "verdict", "started", "completed")
    for i, ph in enumerate(phases, 1):
        t.add_row(
            str(i), ph.phase.value, str(ph.round), ph.status,
            ph.verdict.value if ph.verdict else "—",
            (ph.started_at or "")[-19:],
            (ph.completed_at or "")[-19:],
        )
    console.print(t)
    cur = chain_mod.current_phase(task_db_id)
    if cur:
        gate = " [yellow](awaiting human)[/yellow]" if cur.phase in chain_mod.HUMAN_GATES else ""
        console.print(f"current: [bold]{cur.phase.value}[/bold] round={cur.round} status={cur.status}{gate}")


@chain_app.command("approve-rfc")
def cmd_chain_approve_rfc(project: str, task_number: int, note: str = typer.Option(None, "--note")) -> None:
    pinfo = project_mod.get_project(project)
    if not pinfo:
        raise typer.Exit(2)
    task_db_id = _task_db_id(pinfo["id"], task_number)
    chain_mod.approve_rfc(task_db_id, note=note)
    console.print("[green]RFC approved.[/green]  Launching implementer…")
    launch = chain_mod.run_phase(task_db_id)
    console.print(f"  → {launch.get('phase')} round={launch.get('round')}")


@chain_app.command("reject-rfc")
def cmd_chain_reject_rfc(project: str, task_number: int, reason: str = typer.Option(None, "--reason")) -> None:
    pinfo = project_mod.get_project(project)
    if not pinfo:
        raise typer.Exit(2)
    task_db_id = _task_db_id(pinfo["id"], task_number)
    chain_mod.reject_rfc(task_db_id, reason=reason)
    console.print("[yellow]RFC rejected. Chain terminated.[/yellow]")


@chain_app.command("merge")
def cmd_chain_merge(project: str, task_number: int, yes: bool = typer.Option(False, "--yes")) -> None:
    """Merge the chain's branch into base. Requires the chain to be in pending_merge or conflict."""
    pinfo = project_mod.get_project(project)
    if not pinfo:
        raise typer.Exit(2)
    task_db_id = _task_db_id(pinfo["id"], task_number)
    cur = chain_mod.current_phase(task_db_id)
    if not cur or cur.phase not in (chain_mod.Phase.PENDING_MERGE, chain_mod.Phase.CONFLICT):
        console.print(f"[red]chain not in mergeable state[/red] (current: {cur.phase.value if cur else 'none'})")
        raise typer.Exit(2)
    if not yes:
        if not typer.confirm(f"Merge chain branch for task {task_number:04d}?", default=False):
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(1)
    # The chain uses a single branch named ai/task-NNNN/chain
    branch_worker = "chain"  # not a worker name; merger needs to support raw branch
    try:
        out = merger.merge(task_number, project, branch_worker, confirm=True)
    except merger.MergeAborted as e:
        console.print(f"[red]merge aborted:[/red] {e}")
        raise typer.Exit(2)
    if out.get("merged"):
        chain_mod.mark_merged(task_db_id)
        console.print(f"[green]Merged[/green] {out['branch']}  tests_passed={out.get('tests_passed')}")
    else:
        console.print(f"[red]merge failed:[/red] {out}")


@chain_app.command("reject")
def cmd_chain_reject(project: str, task_number: int, reason: str = typer.Option(None, "--reason")) -> None:
    pinfo = project_mod.get_project(project)
    if not pinfo:
        raise typer.Exit(2)
    task_db_id = _task_db_id(pinfo["id"], task_number)
    chain_mod.reject_task(task_db_id, reason=reason)
    console.print("[yellow]Chain rejected.[/yellow]")


# ---------------------------------------------------------------------------
# Team mode CLI (RFC 0001)
# ---------------------------------------------------------------------------

team_app = typer.Typer(no_args_is_help=True, help="Team mode: planner → specialists → autonomous loops.")
app.add_typer(team_app, name="team")


@team_app.command("run")
def cmd_team_run(
    project: str,
    task: str,
    budget_usd: float = typer.Option(None, "--budget", help="Optional hard cap on rolling cost (USD)."),
) -> None:
    """Start a team task. Spawns the planner; chain pauses at the human approval gate after plan + critique."""
    try:
        out = team_orchestrator.begin_team_task(
            project_name=project, task_text=task, budget_usd=budget_usd,
        )
    except KeyError as e:
        console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(2)
    console.print(
        f"[green]Team task {out['task_number']:04d} started.[/green]  "
        f"planner: {out['planner']} pid={out['planner_pid']}"
    )
    console.print(f"  task folder: {out['task_folder']}")
    console.print(f"Use `johnstudio team status {project} {out['task_number']}` to poll.")
    console.print(f"After plan lands: `johnstudio team approve {project} {out['task_number']}`.")


@team_app.command("status")
def cmd_team_status(project: str, task_number: int) -> None:
    """Show the current state of a team task: phase, plan validity, assignments."""
    pinfo = project_mod.get_project(project)
    if not pinfo:
        raise typer.Exit(2)
    task_db_id = _task_db_id(pinfo["project_id"], task_number)
    state = team_orchestrator.get_team_state(task_db_id)
    console.print(f"[bold]team task[/bold] {task_number:04d} · status: [cyan]{state.get('status')}[/cyan]")
    console.print(f"plan: exists={state.get('plan_exists')} valid={state.get('plan_valid')}")
    if state.get("plan_error"):
        console.print(f"  [red]plan_error:[/red] {state['plan_error']}")
    if state.get("plan_exists") and state.get("plan"):
        plan = state["plan"]
        console.print(f"  summary: {plan.get('summary','')[:120]}")
        t = Table("role", "vp", "brief", "output")
        for a in plan.get("assignments", []):
            t.add_row(a["role"], a["vp"], a["brief"][:60], a["output"])
        console.print(t)
    if state.get("assignments"):
        console.print(f"\n[bold]launched specialists:[/bold] {len(state['assignments'])}")


@team_app.command("plan-critic")
def cmd_team_plan_critic(project: str, task_number: int) -> None:
    """Manually trigger the plan-critic pass (normally auto-triggered by the UI on plan landing)."""
    pinfo = project_mod.get_project(project)
    if not pinfo:
        raise typer.Exit(2)
    task_db_id = _task_db_id(pinfo["project_id"], task_number)
    out = team_orchestrator.run_plan_critic(task_db_id)
    console.print(out)


@team_app.command("approve")
def cmd_team_approve(project: str, task_number: int) -> None:
    """Approve the plan and spawn all named specialists in parallel."""
    pinfo = project_mod.get_project(project)
    if not pinfo:
        raise typer.Exit(2)
    task_db_id = _task_db_id(pinfo["project_id"], task_number)
    try:
        out = team_orchestrator.approve_plan_and_run(task_db_id)
    except (RuntimeError, team_mod.PlanError) as e:
        console.print(f"[red]approve failed:[/red] {e}")
        raise typer.Exit(2)
    if out.get("refused"):
        console.print(f"[red]refused:[/red] {out.get('reason')}")
        raise typer.Exit(2)
    if out.get("already_running"):
        console.print(f"[yellow]already running.[/yellow]")
        return
    if out.get("plan_invalid"):
        console.print(f"[red]plan invalid ({out.get('stage')}):[/red] {out.get('error')}")
        console.print(f"[dim]re-issue the planner: johnstudio team replan {task_number}[/dim]")
        raise typer.Exit(2)
    if out.get("accepted"):
        # Spawn now runs in the background (returns 202 immediately); follow via status/SSE.
        console.print("[green]approved — team spawning in the background.[/green]")
        console.print(f"[dim]follow progress: johnstudio team status {task_number}[/dim]")
        return
    # Legacy synchronous shape (older code paths that still return 'launched').
    launched = out.get("launched", [])
    console.print(f"[green]spawned {len(launched)} specialists.[/green]")
    t = Table("role", "vp", "run_id", "pid", "worktree")
    for a in launched:
        t.add_row(a["role"], a["vp"], str(a["run_id"]), str(a.get("pid")), a.get("worktree") or "")
    console.print(t)


@team_app.command("replan")
def cmd_team_replan(project: str, task_number: int) -> None:
    """Re-issue the lead planner after an invalid plan parked at 'needs_replan'."""
    pinfo = project_mod.get_project(project)
    if not pinfo:
        raise typer.Exit(2)
    task_db_id = _task_db_id(pinfo["project_id"], task_number)
    try:
        out = team_orchestrator.replan_team_task(task_db_id)
    except (RuntimeError, team_mod.PlanError) as e:
        console.print(f"[red]replan failed:[/red] {e}")
        raise typer.Exit(2)
    if out.get("refused"):
        console.print(f"[red]refused:[/red] {out.get('reason')}")
        raise typer.Exit(2)
    console.print("[green]planner re-issued — task reset to 'planning'.[/green]")
    console.print(f"[dim]when the new plan lands, run: johnstudio team approve {project} {task_number}[/dim]")


@team_app.command("cancel")
def cmd_team_cancel(project: str, task_number: int) -> None:
    """Cancel a running team task: kill all live specialists, mark the task cancelled. Idempotent."""
    pinfo = project_mod.get_project(project)
    if not pinfo:
        raise typer.Exit(2)
    task_db_id = _task_db_id(pinfo["project_id"], task_number)
    try:
        out = team_orchestrator.cancel_team_task(task_db_id)
    except (KeyError, RuntimeError) as e:
        console.print(f"[red]cancel failed:[/red] {e}")
        raise typer.Exit(2)
    n = out.get("count", 0)
    console.print(f"[green]task {task_number:04d} cancelled — stopped {n} specialist(s).[/green]")
    if n:
        t = Table("run_id", "worker", "pid", "killed")
        for c in out.get("cancelled", []):
            t.add_row(str(c["run_id"]), c["worker"], str(c.get("pid")), str(c.get("killed")))
        console.print(t)


@team_app.command("advance")
def cmd_team_advance(project: str, task_number: int) -> None:
    """Manually tick the state machine (normally the 5s background ticker does this)."""
    pinfo = project_mod.get_project(project)
    if not pinfo:
        raise typer.Exit(2)
    task_db_id = _task_db_id(pinfo["project_id"], task_number)
    out = team_orchestrator.advance_team_task(task_db_id)
    console.print(out)


@team_app.command("budget")
def cmd_team_budget(project: str, task_number: int) -> None:
    """Report cost + budget posture for a team task."""
    pinfo = project_mod.get_project(project)
    if not pinfo:
        raise typer.Exit(2)
    task_db_id = _task_db_id(pinfo["project_id"], task_number)
    bs = team_orchestrator.check_budget(task_db_id)
    color = "red" if bs.get("over_budget") else "green"
    budget = bs.get("budget_usd")
    budget_str = f"${budget:.2f}" if budget is not None else "—"
    console.print(
        f"cost: [{color}]${bs.get('cost_usd', 0):.4f}[/{color}]  budget: {budget_str}  "
        f"{'[red]over budget[/red]' if bs.get('over_budget') else '[green]ok[/green]'}"
    )


@team_app.command("catalog")
def cmd_team_catalog() -> None:
    """List every role in the team catalog grouped by VP."""
    catalog = team_mod.load_role_catalog()
    by_vp = team_mod.roles_by_vp(catalog)
    for vp, roles in by_vp.items():
        console.print(f"[bold]{vp}[/bold]  ({len(roles)} roles)")
        t = Table("name", "can_edit", "model", "description", show_lines=False)
        for r in roles:
            t.add_row(r.name, "✓" if r.can_edit else "—", r.model or "—", r.description[:80])
        console.print(t)
        console.print()


def _task_db_id(project_id: int, task_number: int) -> int:
    from . import db as _db
    conn = _db.connect()
    _db.init_schema(conn)
    row = conn.execute(
        "SELECT id FROM tasks WHERE project_id = ? AND task_number = ?",
        (project_id, task_number),
    ).fetchone()
    conn.close()
    if not row:
        console.print(f"[red]no task[/red] {task_number:04d} in project {project_id}")
        raise typer.Exit(2)
    return int(row["id"])


if __name__ == "__main__":
    app()
