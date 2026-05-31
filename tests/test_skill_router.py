from __future__ import annotations

from johnstudio import config, project as project_mod, skill_importer, skill_router
from johnstudio.models import ProjectStack


def _setup(jh_home, git_repo, *, frameworks=None):
    project_mod.add_project("demo", git_repo)
    skill_importer.import_seeds()
    pcfg = config.load_project_config(git_repo)
    if frameworks:
        pcfg.stack = ProjectStack(
            languages=["typescript", "javascript"],
            frameworks=frameworks,
        )
        config.write_project_config(pcfg)
    return pcfg


def test_route_picks_frontend_skill_for_react_task(jh_home, git_repo):
    pcfg = _setup(jh_home, git_repo, frameworks=["react", "nextjs"])
    req = skill_router.RouteRequest(
        project=pcfg,
        agent_role="frontend_implementer",
        task_text="Build a Next.js login page",
        relevant_files=["app/login/page.tsx"],
    )
    sel = skill_router.route(req)
    ids = {s.skill_id for s in sel}
    assert "frontend-react-specialist" in ids


def test_route_picks_security_skill_for_billing_task(jh_home, git_repo):
    pcfg = _setup(jh_home, git_repo)
    req = skill_router.RouteRequest(
        project=pcfg,
        agent_role="security_reviewer",
        task_text="Add Stripe billing and review for auth/CSRF risks",
    )
    sel = skill_router.route(req)
    ids = {s.skill_id for s in sel}
    assert "security-auditor" in ids


def test_route_respects_max_skills_per_agent(jh_home, git_repo):
    pcfg = _setup(jh_home, git_repo, frameworks=["react", "nextjs"])
    req = skill_router.RouteRequest(
        project=pcfg,
        agent_role="backend_implementer",
        task_text="Add a Next.js hello endpoint with tests and security review for auth",
        relevant_files=["app/api/hello/route.ts"],
    )
    sel = skill_router.route(req)
    assert len(sel) <= 6  # default max_skills_per_agent


def test_route_skips_disabled_skills(jh_home, git_repo):
    pcfg = _setup(jh_home, git_repo, frameworks=["react"])
    # Disable everything except security-auditor.
    from johnstudio import skill_registry
    for r in skill_registry.list_skills():
        if r["skill_id"] != "security-auditor":
            skill_registry.set_enabled(r["skill_id"], False)
    req = skill_router.RouteRequest(
        project=pcfg,
        agent_role="frontend_implementer",
        task_text="Build a React component",
    )
    sel = skill_router.route(req)
    ids = {s.skill_id for s in sel}
    assert "frontend-react-specialist" not in ids


def test_route_returns_skills_with_positive_score_only(jh_home, git_repo):
    pcfg = _setup(jh_home, git_repo)
    req = skill_router.RouteRequest(
        project=pcfg,
        agent_role="random_role",
        task_text="completely unrelated content xyzzy",
    )
    sel = skill_router.route(req)
    for s in sel:
        assert s.score > 0


def test_route_for_demo_project_returns_balanced_team(jh_home, git_repo):
    pcfg = _setup(jh_home, git_repo, frameworks=["react", "nextjs"])
    req = skill_router.RouteRequest(
        project=pcfg,
        agent_role="lead_planner",
        task_text="build a Next.js login page with tests and security review",
        relevant_files=["app/login/page.tsx", "app/api/auth/route.ts"],
    )
    sel = skill_router.route(req)
    cats = {s.skill_id for s in sel}
    # We expect at least one of each: frontend, testing, security.
    assert any("react" in c or "frontend" in c for c in cats)
    assert any("test" in c for c in cats)
    assert any("security" in c for c in cats)
