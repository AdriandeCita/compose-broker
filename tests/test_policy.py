import os
from pathlib import Path

from compose_broker.policy import check_policy
from tests.helpers import Env


class PolicyTests(Env):
    def errors(self, cfg):
        return [f for f in check_policy(cfg, self.p) if f.level == "error"]

    def test_good_config_passes(self):
        self.assertEqual(self.errors(self.good_config()), [])

    def test_forbidden_service_keys(self):
        for key, val in (("privileged", True), ("cap_add", ["SYS_ADMIN"]),
                         ("devices", ["/dev/sda"]), ("use_api_socket", True),
                         ("provider", {"type": "x"}), ("container_name", "x"),
                         ("volumes_from", ["other"]), ("some_future_key", 1)):
            cfg = self.good_config()
            cfg["services"]["db"][key] = val
            self.assertTrue(self.errors(cfg), key)

    def test_host_namespaces(self):
        for key, val in (("network_mode", "host"), ("pid", "host"), ("ipc", "host"),
                         ("network_mode", "container:abc"), ("ipc", "container:abc")):
            cfg = self.good_config()
            cfg["services"]["db"][key] = val
            self.assertTrue(self.errors(cfg), (key, val))
        cfg = self.good_config()
        cfg["services"]["db"]["network_mode"] = "service:web"
        self.assertEqual(self.errors(cfg), [])

    def test_bind_mounts_only_project_root(self):
        for src in ("/", "/var/run/docker.sock", str(Path.home()), self.p.dir + "/src",
                    os.path.dirname(self.p.dir)):
            cfg = self.good_config()
            cfg["services"]["web"]["volumes"][0]["source"] = src
            self.assertTrue(self.errors(cfg), src)

    def test_volume_driver_opts_and_names(self):
        cfg = self.good_config()
        cfg["volumes"]["data"]["driver_opts"] = {"type": "none", "o": "bind", "device": "/"}
        self.assertTrue(self.errors(cfg))
        cfg = self.good_config()
        cfg["volumes"]["data"]["name"] = "cb-other_data"
        self.assertTrue(self.errors(cfg))
        cfg = self.good_config()
        cfg["volumes"]["data"]["external"] = True
        self.assertTrue(self.errors(cfg))

    def test_build_restrictions(self):
        for mutate in (lambda b: b.update(context="/"),
                       lambda b: b.update(context=os.path.dirname(self.p.dir)),
                       lambda b: b.update(context=self.p.dir + "-evil"),
                       lambda b: b.update(context=self.p.dir + "/sub/../../x"),
                       lambda b: b.update(context="https://github.com/x/y.git#main"),
                       lambda b: b.update(context="sub"),
                       lambda b: b.update(dockerfile="../evil/Dockerfile"),
                       lambda b: b.update(context=self.p.dir + "/sub",
                                          dockerfile="../../evil/Dockerfile"),
                       lambda b: b.update(ssh=["default"]),
                       lambda b: b.update(network="host"),
                       lambda b: b.update(additional_contexts={"x": "/etc"}),
                       lambda b: b.update(tags=["postgres:16"])):
            cfg = self.good_config()
            mutate(cfg["services"]["web"]["build"])
            self.assertTrue(self.errors(cfg))

    def test_nested_build_context_allowed(self):
        # POL-S11: any directory inside the project; Dockerfile relative to it,
        # and may sit outside the context as long as it stays in the project.
        for ctx, dockerfile in (("/frontend", "Dockerfile"), ("/apps/web", "../../docker/web.Dockerfile"),
                                ("/frontend", None), ("/backend", "/backend/Dockerfile")):
            cfg = self.good_config()
            build = cfg["services"]["web"]["build"]
            build["context"] = self.p.dir + ctx
            build.pop("dockerfile")
            if dockerfile is not None:
                build["dockerfile"] = self.p.dir + dockerfile if dockerfile.startswith("/") \
                    else dockerfile
            self.assertEqual(self.errors(cfg), [], (ctx, dockerfile))

    def test_built_image_name_prefix(self):
        cfg = self.good_config()
        cfg["services"]["web"]["image"] = "postgres:16"
        self.assertTrue(self.errors(cfg))
        cfg["services"]["web"]["image"] = "cb-shop-web:dev"
        self.assertEqual(self.errors(cfg), [])

    def test_secret_file_sources(self):
        cfg = self.good_config()
        cfg["secrets"] = {"key": {"file": "/home/me/.ssh/id_rsa"}}
        self.assertTrue(self.errors(cfg))
        cfg["secrets"] = {"key": {"environment": "API_KEY"}}
        self.assertEqual(self.errors(cfg), [])

    def test_compose_labels_and_hooks(self):
        cfg = self.good_config()
        cfg["services"]["db"]["labels"] = {"com.docker.compose.project": "cb-other"}
        self.assertTrue(self.errors(cfg))
        # SBX-7: the agent must not label its containers as broker sandboxes.
        for labels in ({"com.compose-broker.sandbox": "other"}, ["com.compose-broker.op=up"]):
            cfg = self.good_config()
            cfg["services"]["db"]["labels"] = labels
            self.assertTrue(self.errors(cfg), labels)
        cfg = self.good_config()
        cfg["services"]["web"]["build"]["labels"] = {"com.compose-broker.sandbox": "other"}
        self.assertTrue(self.errors(cfg))
        cfg = self.good_config()
        cfg["services"]["db"]["post_start"] = [{"command": "id", "privileged": True}]
        self.assertTrue(self.errors(cfg))

    def warnings(self, cfg):
        return [f for f in check_policy(cfg, self.p) if f.level == "warning"]

    def test_root_with_writable_project_mount_is_warning(self):
        # POL-S13
        for user in (None, "", "0", "root", "0:0", "root:staff"):
            cfg = self.good_config()
            if user is None:
                del cfg["services"]["web"]["user"]
            else:
                cfg["services"]["web"]["user"] = user
            self.assertEqual([f.where for f in self.warnings(cfg)], ["services.web.user"], user)
            self.assertEqual(self.errors(cfg), [])
        for user in ("1000", "1000:1000", "node", "app:app"):
            cfg = self.good_config()
            cfg["services"]["web"]["user"] = user
            self.assertEqual(self.warnings(cfg), [], user)
        cfg = self.good_config()  # read-only mount: nothing can be written
        del cfg["services"]["web"]["user"]
        cfg["services"]["web"]["volumes"][0]["read_only"] = True
        self.assertEqual(self.warnings(cfg), [])
        cfg = self.good_config()  # no project mount at all (db service)
        self.assertNotIn("services.db.user", [f.where for f in self.warnings(cfg)])

    def test_root_hook_with_writable_project_mount_is_warning(self):
        cfg = self.good_config()
        cfg["services"]["web"]["post_start"] = [{"command": "chown -R x /workspace", "user": "root"}]
        self.assertEqual([f.where for f in self.warnings(cfg)], ["services.web.post_start[0].user"])
        cfg["services"]["web"]["post_start"] = [{"command": "true"}]  # inherits user 1000
        self.assertEqual(self.warnings(cfg), [])

    def test_wildcard_port_is_warning(self):
        cfg = self.good_config()
        del cfg["services"]["web"]["ports"][0]["host_ip"]
        findings = check_policy(cfg, self.p)
        self.assertEqual([f.level for f in findings], ["warning"])
