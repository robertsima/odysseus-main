"""The chassis that makes a fresh install behave.

Two invariants, both of which the codebase violated before this:

1. **A tool the host cannot run is not offered to the model.** Previously
   `delegate_to_claude_code` sat in the schema whether or not a binary existed,
   so on a fresh install the model picked it and the call failed at the far end.
   "Off" and "impossible here" must also stay distinguishable, or the operator
   cannot tell a switch they can flip from one they cannot.

2. **A setting with no schema entry has no UI control.** That is how the settings
   page fell 17 keys behind. `missing_specs()` staying empty is what makes
   adding a control mandatory rather than optional.
"""

import pytest


@pytest.fixture(autouse=True)
def _fresh_probes():
    from src import capabilities

    capabilities.invalidate_probes()
    yield
    capabilities.invalidate_probes()


class TestCapabilityMechanism:
    def test_a_capability_with_no_requirements_is_satisfied(self):
        from src import capabilities

        cap = capabilities.Capability(
            name="_t_plain", title="Plain", summary="", default_enabled=True,
        )
        capabilities.register(cap)
        st = capabilities.status("_t_plain")
        assert st.satisfied is True and st.available is True

    def test_unmet_requirement_makes_it_unavailable_with_a_reason(self):
        """The operator needs to know what to install, not just that it failed."""
        from src import capabilities

        capabilities.register(capabilities.Capability(
            name="_t_needs", title="Needs", summary="", default_enabled=True,
            requirements=(capabilities.Requirement(
                name="a thing",
                check=lambda: (False, "the thing is absent"),
                hint="install the thing",
            ),),
        ))
        st = capabilities.status("_t_needs")
        assert st.satisfied is False and st.available is False
        assert st.unmet[0]["detail"] == "the thing is absent"
        assert st.unmet[0]["hint"] == "install the thing"

    def test_disabled_but_satisfied_is_distinct_from_unsatisfied(self):
        """'You turned this off' and 'this host cannot do it' are different
        problems with different fixes, so they stay separate fields."""
        from src import capabilities

        capabilities.register(capabilities.Capability(
            name="_t_off", title="Off", summary="", default_enabled=False,
        ))
        st = capabilities.status("_t_off")
        assert st.satisfied is True
        assert st.enabled is False
        assert st.available is False

    def test_a_raising_probe_fails_closed_without_propagating(self):
        """Probes run while building the tool schema on every round; one that
        throws must not take the turn down, and must not report success."""
        from src import capabilities

        def boom():
            raise RuntimeError("probe exploded")

        capabilities.register(capabilities.Capability(
            name="_t_boom", title="Boom", summary="", default_enabled=True,
            requirements=(capabilities.Requirement(name="explodes", check=boom),),
        ))
        st = capabilities.status("_t_boom")
        assert st.satisfied is False
        assert "probe exploded" in st.unmet[0]["detail"]

    def test_probe_results_are_cached(self):
        """The schema is rebuilt every round; a shell-out must not run per round."""
        from src import capabilities

        calls = {"n": 0}

        def counting():
            calls["n"] += 1
            return True, "ok"

        capabilities.register(capabilities.Capability(
            name="_t_cache", title="Cache", summary="", default_enabled=True,
            requirements=(capabilities.Requirement(name="counted", check=counting),),
        ))
        for _ in range(5):
            capabilities.status("_t_cache")
        assert calls["n"] == 1
        capabilities.invalidate_probes()
        capabilities.status("_t_cache")
        assert calls["n"] == 2


class TestToolWithholding:
    def test_unavailable_capability_withholds_its_tools(self):
        from src import capabilities

        capabilities.register(capabilities.Capability(
            name="_t_hidden", title="Hidden", summary="", default_enabled=True,
            tools=("_t_tool_a", "_t_tool_b"),
            requirements=(capabilities.Requirement(
                name="missing", check=lambda: (False, "nope")),),
        ))
        assert {"_t_tool_a", "_t_tool_b"} <= capabilities.unavailable_tools()

    def test_available_capability_does_not_withhold(self):
        from src import capabilities

        capabilities.register(capabilities.Capability(
            name="_t_shown", title="Shown", summary="", default_enabled=True,
            tools=("_t_tool_c",),
        ))
        assert "_t_tool_c" not in capabilities.unavailable_tools()

    def test_a_tool_belonging_to_no_capability_is_never_withheld(self):
        """Otherwise adding the registry would silently delete every tool that
        had not yet been claimed by a capability."""
        from src import capabilities

        assert "read_file" not in capabilities.unavailable_tools()

    def test_unknown_capability_is_treated_as_available(self):
        """A typo in a capability name must not silently remove functionality."""
        from src import capabilities

        assert capabilities.is_available("no_such_capability_xyz") is True


class TestBuiltinDeclarations:
    def test_host_control_capabilities_are_off_by_default(self):
        """Anything that hands a model the host, or reaches another machine,
        must require an explicit decision — not inherit one from the author."""
        import src.capabilities_builtin  # noqa: F401  (registers)
        from src import capabilities

        for name in ("host_docker", "remote_hosts", "model_serving"):
            cap = capabilities.get(name)
            assert cap is not None, name
            assert cap.default_enabled is False, f"{name} must be opt-in"

    def test_local_worktrees_are_not_gated_behind_publishing(self):
        """Gating the worktree tool on "publishing is risky" would have removed
        local worktrees from every default install — a capability regression
        dressed up as caution. Creating a worktree and committing to a branch is
        ordinary local work; *pushing* is the part needing a decision, and that
        already has its own gate (ODYSSEUS_AGENT_PUBLISH_ENABLED, enforced in
        src/agent_worktree/config.py)."""
        import src.capabilities_builtin  # noqa: F401
        from src import capabilities

        cap = capabilities.get("agent_worktrees")
        assert cap is not None, "agent_worktrees capability is missing"
        assert cap.default_enabled is True
        assert "manage_agent_worktree" in cap.tools
        # And the old over-broad capability must be gone, not merely unused.
        assert capabilities.get("worktree_publish") is None

    def test_image_runners_live_with_model_serving(self):
        """DDColor / inpaint / MLX image generation are serve targets for image
        models, not standalone features — they are gated by Cookbook, so there
        is no separate switch that can contradict it."""
        import src.capabilities_builtin  # noqa: F401
        from src import capabilities

        assert capabilities.get("model_serving") is not None
        assert capabilities.get("image_pipelines") is None

    def test_delegation_requirement_names_a_non_api_key_route(self):
        """A subscription is billed as a subscription; an API key is billed per
        token. The hint must not push an operator onto the metered path."""
        import src.capabilities_builtin  # noqa: F401
        from src import capabilities

        cap = capabilities.get("code_delegation")
        hint = " ".join(r.hint for r in cap.requirements).lower()
        assert "subscription" in hint
        assert "api key" in hint  # explicitly says one is *not* required


class TestSettingsSchema:
    def test_every_setting_has_a_control(self):
        """Empty is the invariant: a new setting without a schema entry fails
        here instead of quietly having no UI."""
        from src import settings_schema

        assert settings_schema.missing_specs() == []

    def test_new_knowledge_settings_are_declared_and_defaulted(self):
        from src.settings import DEFAULT_SETTINGS
        from src import settings_schema

        for key in ("vault_directory", "notes_directory", "notes_archive_directory",
                    "vault_default_sensitivity", "vault_folder_sensitivity"):
            assert key in DEFAULT_SETTINGS, key
            assert settings_schema.get_spec(key) is not None, key

    def test_notes_directory_is_configurable_and_relative(self):
        """The operator picks where notes live; it is a path inside the vault so
        one privacy policy covers it."""
        from src.settings import DEFAULT_SETTINGS
        from src import settings_schema

        spec = settings_schema.get_spec("notes_directory")
        assert spec.group == "Knowledge"
        assert DEFAULT_SETTINGS["notes_directory"] == "Notes"
        assert not DEFAULT_SETTINGS["notes_directory"].startswith("/")

    def test_default_sensitivity_is_folder_wide_and_safe(self):
        from src.settings import DEFAULT_SETTINGS
        from src import settings_schema

        spec = settings_schema.get_spec("vault_default_sensitivity")
        assert spec.choices == ("public", "private")
        assert DEFAULT_SETTINGS["vault_default_sensitivity"] in ("public", "private")

    def test_secrets_are_not_echoed_back(self):
        from src import settings_schema

        spec = settings_schema.SettingSpec(
            key="x_api_key", type="secret", label="Key", sensitive=True,
        )
        rendered = spec.as_dict("s3cret-value")
        assert "s3cret-value" not in str(rendered)
        assert rendered["value"] == "********"

    def test_env_pinned_settings_render_locked(self, monkeypatch):
        """A deployment may pin a value; the UI must say so rather than accept a
        click that will not stick."""
        from src import settings_schema

        spec = settings_schema.SettingSpec(
            key="x_dir", type="path", label="Dir", env_override="X_ODY_TEST_DIR",
        )
        monkeypatch.delenv("X_ODY_TEST_DIR", raising=False)
        assert settings_schema.env_locked(spec) is False
        monkeypatch.setenv("X_ODY_TEST_DIR", "/somewhere")
        assert settings_schema.env_locked(spec) is True

    def test_ui_payload_is_grouped_and_hides_admin_only_from_users(self):
        from src import settings_schema

        admin = settings_schema.ui_payload(is_admin=True)
        user = settings_schema.ui_payload(is_admin=False)
        assert admin and all("group" in g and "settings" in g for g in admin)
        admin_count = sum(len(g["settings"]) for g in admin)
        user_count = sum(len(g["settings"]) for g in user)
        assert user_count < admin_count
