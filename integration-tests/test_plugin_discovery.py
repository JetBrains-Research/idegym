"""Integration tests for the plugin discovery system.

Verifies that all three plugin extension points (image-build, server, client) are
properly discoverable via ``importlib.metadata`` entry_points, that each plugin's
entry_point loads the correct class, and that the resulting FastAPI routers expose
the expected endpoint paths.

These tests exercise the real installed package metadata and do NOT require Docker,
Kubernetes, or any external services.
"""

from importlib.metadata import entry_points

# ---------------------------------------------------------------------------
# idegym.plugins.image — image-build plugins
# ---------------------------------------------------------------------------


def test_entry_points_image_group_discovers_all_expected_plugins():
    """All built-in and optional image plugins are reachable via the image entry_points group."""
    eps = {ep.name for ep in entry_points(group="idegym.plugins.image")}
    expected = {"base-system", "user", "permissions", "mcp-upstream", "project", "idegym-server", "pycharm"}
    missing = expected - eps
    assert not missing, f"Missing image plugin entry_points: {missing}"


def test_entry_points_image_group_loads_correct_classes():
    """Each image entry_point loads to the exact plugin class it declares."""
    from idegym.plugins.defaults.image import BaseSystem, IdeGYMServer, MCPUpstream, Permissions, Project, User
    from idegym.plugins.pycharm.image import PyCharm

    expected_classes = {
        "base-system": BaseSystem,
        "user": User,
        "permissions": Permissions,
        "mcp-upstream": MCPUpstream,
        "project": Project,
        "idegym-server": IdeGYMServer,
        "pycharm": PyCharm,
    }
    eps = {ep.name: ep for ep in entry_points(group="idegym.plugins.image")}
    for name, cls in expected_classes.items():
        loaded = eps[name].load()
        assert loaded is cls, f"Entry point 'idegym.plugins.image:{name}' loaded {loaded!r}, expected {cls!r}"


def test_image_plugin_entry_points_are_subclasses_of_plugin_base():
    """Every class loaded from idegym.plugins.image is a subclass of PluginBase."""
    from idegym.api.plugin import PluginBase

    for ep in entry_points(group="idegym.plugins.image"):
        cls = ep.load()
        assert issubclass(cls, PluginBase), f"Image plugin '{ep.name}' ({cls!r}) is not a subclass of PluginBase"


def test_image_plugin_entry_points_are_registered_in_registry():
    """Loading image entry_points populates the @image_plugin registry with their names."""
    from idegym.api.plugin import get_plugin_class

    # builder.py already triggers loading on import; force it for isolation
    for ep in entry_points(group="idegym.plugins.image"):
        ep.load()

    for ep in entry_points(group="idegym.plugins.image"):
        assert get_plugin_class(ep.name) is not None, (
            f"Plugin '{ep.name}' not found in @image_plugin registry after loading"
        )


# ---------------------------------------------------------------------------
# idegym.plugins.server — server-side router plugins
# ---------------------------------------------------------------------------


def test_entry_points_server_group_discovers_expected_plugins():
    """tools, rewards, and pycharm are all reachable via the server entry_points group."""
    eps = {ep.name for ep in entry_points(group="idegym.plugins.server")}
    expected = {"tools", "rewards", "pycharm"}
    missing = expected - eps
    assert not missing, f"Missing server plugin entry_points: {missing}"


def test_entry_points_server_group_loads_classes_with_get_server_router():
    """Every class loaded from idegym.plugins.server exposes a get_server_router() classmethod."""
    for ep in entry_points(group="idegym.plugins.server"):
        cls = ep.load()
        assert callable(getattr(cls, "get_server_router", None)), (
            f"Server plugin '{ep.name}' ({cls!r}) does not have a callable get_server_router()"
        )


def test_tools_plugin_router_exposes_expected_endpoints():
    """ToolsPlugin.get_server_router() returns a FastAPI router with all tools paths."""
    from idegym.api.paths import ToolsPath

    eps = {ep.name: ep for ep in entry_points(group="idegym.plugins.server")}
    tools_cls = eps["tools"].load()
    router = tools_cls.get_server_router()

    route_paths = {r.path for r in router.routes}
    assert ToolsPath.BASH in route_paths, f"/tools/bash missing from router paths: {route_paths}"
    assert ToolsPath.CREATE_FILE in route_paths
    assert ToolsPath.EDIT_FILE in route_paths
    assert ToolsPath.PATCH_FILE in route_paths


def test_rewards_plugin_router_exposes_expected_endpoints():
    """RewardsPlugin.get_server_router() returns a FastAPI router with all rewards paths."""
    from idegym.api.paths import RewardsPath

    eps = {ep.name: ep for ep in entry_points(group="idegym.plugins.server")}
    rewards_cls = eps["rewards"].load()
    router = rewards_cls.get_server_router()

    route_paths = {r.path for r in router.routes}
    assert RewardsPath.COMPILATION in route_paths, f"/rewards/compilation missing: {route_paths}"
    assert RewardsPath.SETUP in route_paths
    assert RewardsPath.TEST in route_paths


def test_pycharm_server_plugin_router_exposes_health_endpoint():
    """PyCharmPlugin.get_server_router() returns a router with GET /pycharm/health."""
    eps = {ep.name: ep for ep in entry_points(group="idegym.plugins.server")}
    pycharm_cls = eps["pycharm"].load()
    router = pycharm_cls.get_server_router()

    route_paths = {r.path for r in router.routes}
    assert "/pycharm/health" in route_paths, f"/pycharm/health missing from pycharm router: {route_paths}"


def test_loading_server_entry_points_populates_server_registry():
    """Loading server entry_points adds their classes to the @server_plugin registry."""
    from idegym.api.plugin import get_all_server_plugins

    for ep in entry_points(group="idegym.plugins.server"):
        ep.load()

    all_server = get_all_server_plugins()
    all_names = {cls.__name__ for cls in all_server}
    assert "ToolsPlugin" in all_names, f"ToolsPlugin missing from server registry: {all_names}"
    assert "RewardsPlugin" in all_names, f"RewardsPlugin missing from server registry: {all_names}"
    assert "PyCharmPlugin" in all_names, f"PyCharmPlugin missing from server registry: {all_names}"


# ---------------------------------------------------------------------------
# idegym.plugins.client — client-side operations plugins
# ---------------------------------------------------------------------------


def test_entry_points_client_group_discovers_pycharm():
    """pycharm is reachable via the idegym.plugins.client entry_points group."""
    eps = {ep.name for ep in entry_points(group="idegym.plugins.client")}
    assert "pycharm" in eps, f"'pycharm' not in client entry_points: {eps}"


def test_pycharm_client_entry_point_loads_correct_class():
    """The 'pycharm' client entry_point resolves to PycharmClientOperations."""
    from idegym.plugins.pycharm.client import PycharmClientOperations

    eps = {ep.name: ep for ep in entry_points(group="idegym.plugins.client")}
    loaded = eps["pycharm"].load()
    assert loaded is PycharmClientOperations


# ---------------------------------------------------------------------------
# Server plugin config-based filtering (simulates server/main.py logic)
# ---------------------------------------------------------------------------


def test_server_plugin_filtering_loads_only_configured_plugins():
    """Simulates server startup: only entry_points listed in a config set are loaded."""
    # Load only tools and rewards — deliberately exclude pycharm
    enabled = {"tools", "rewards"}
    loaded_classes = []
    for ep in entry_points(group="idegym.plugins.server"):
        if ep.name in enabled:
            loaded_classes.append(ep.load())

    loaded_names = {cls.__name__ for cls in loaded_classes}
    assert "ToolsPlugin" in loaded_names
    assert "RewardsPlugin" in loaded_names
    assert "PyCharmPlugin" not in loaded_names


def test_server_plugin_all_available_when_no_config_filter():
    """Without config-based filtering, all installed server plugins are discoverable."""
    all_ep_names = {ep.name for ep in entry_points(group="idegym.plugins.server")}
    assert "tools" in all_ep_names
    assert "rewards" in all_ep_names
    assert "pycharm" in all_ep_names


def test_server_plugin_unknown_name_in_config_is_silently_skipped():
    """An unrecognised plugin name in the config set does not cause errors."""
    # Simulate a config that includes a non-existent plugin name
    enabled = {"tools", "rewards", "nonexistent-plugin"}
    loaded_classes = []
    for ep in entry_points(group="idegym.plugins.server"):
        if ep.name in enabled:
            loaded_classes.append(ep.load())

    # The unknown name is simply never matched — no error raised
    loaded_names = {cls.__name__ for cls in loaded_classes}
    assert "ToolsPlugin" in loaded_names
    assert "RewardsPlugin" in loaded_names
