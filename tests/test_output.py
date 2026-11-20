import unittest

from compose_broker.output import MAX_LINES, build_failed, condense

# Real `docker compose up -d --build --wait` output from the demo stack (abridged).
SUCCESS = """\
 Image cb-demo-ticker Building
#1 [internal] load local bake definitions
#1 reading from stdin 597B done
#1 DONE 0.0s

#3 [internal] load metadata for docker.io/library/busybox:1.36
#3 DONE 1.9s

#6 [1/2] FROM docker.io/library/busybox:1.36@sha256:73aaf090f3d8
#6 sha256:034d6572bf28 1.05MB / 2.21MB 0.4s
#6 sha256:034d6572bf28 2.21MB / 2.21MB 0.5s done
#6 extracting sha256:034d6572bf28 0.1s done
#6 DONE 0.6s

#7 [2/2] COPY ticker/ticker.sh /ticker.sh
#7 DONE 0.0s

#8 exporting to image
#8 naming to docker.io/library/cb-demo-ticker done
#8 DONE 0.1s
 Image cb-demo-ticker Built
 Network cb-demo_default Creating
 Network cb-demo_default Creating
 Network cb-demo_default Created
 Network cb-demo_default Created
 Container cb-demo-ticker-1 Creating
 Container cb-demo-ticker-1 Created
 Container cb-demo-ticker-1 Starting
 Container cb-demo-ticker-1 Started
 Container cb-demo-ticker-1 Waiting
 Container cb-demo-ticker-1 Healthy
"""

BUILD_FAILURE = """\
 Image cb-demo-api Building
#5 [1/3] FROM docker.io/library/node:22
#5 DONE 0.1s

#6 [2/3] RUN npm ci
#6 0.512 added 120 packages in 3s
#6 DONE 3.4s

#7 [3/3] RUN npm run build
#7 0.301 > tsc -p .
#7 2.114 src/index.ts(3,7): error TS2322: Type 'string' is not assignable to type 'number'.
#7 ERROR: process "/bin/sh -c npm run build" did not complete successfully: exit code: 2
------
 > [3/3] RUN npm run build:
2.114 src/index.ts(3,7): error TS2322: Type 'string' is not assignable to type 'number'.
------
failed to solve: process "/bin/sh -c npm run build" did not complete successfully: exit code: 2
"""


class CondenseTests(unittest.TestCase):
    def test_success_keeps_final_state_per_object(self):
        self.assertEqual(condense(SUCCESS), "\n".join([
            "Image cb-demo-ticker: Built",
            "Network cb-demo_default: Created",
            "Container cb-demo-ticker-1: Healthy",
        ]))

    def test_build_failure_keeps_failed_step_and_errors(self):
        text = condense(BUILD_FAILURE)
        self.assertIn("#7 2.114 src/index.ts(3,7): error TS2322", text)
        self.assertIn("#7 ERROR: process", text)
        self.assertIn("failed to solve:", text)
        self.assertIn("Image cb-demo-api: Building", text)
        self.assertNotIn("npm ci", text)          # successful steps dropped
        self.assertNotIn("#5", text)

    def test_start_failure_keeps_compose_errors(self):
        out = (" Container cb-demo-db-1 Starting \n"
               " Container cb-demo-db-1 Error \n"
               "dependency failed to start: container cb-demo-db-1 exited (1)\n")
        self.assertEqual(condense(out), "Container cb-demo-db-1: Error\n"
                         "dependency failed to start: container cb-demo-db-1 exited (1)")

    def test_warnings_and_unknown_lines_kept(self):
        out = 'time="2026" level=warning msg="version is obsolete"\n Container x-1 Started \n'
        self.assertIn("level=warning", condense(out))

    def test_caps_lines(self):
        out = "\n".join(f"error line {i}" for i in range(MAX_LINES + 30))
        text = condense(out)
        self.assertTrue(text.startswith("[... 30 earlier lines omitted ...]"))
        self.assertIn(f"error line {MAX_LINES + 29}", text)
        self.assertNotIn("error line 29\n", text)

    def test_empty(self):
        self.assertEqual(condense(""), "(no output)")
        self.assertEqual(condense("#1 DONE 0.0s\n"), "(no output)")

    def test_build_failed(self):
        self.assertTrue(build_failed(BUILD_FAILURE))
        self.assertTrue(build_failed("failed to solve: failed to read dockerfile\n"))
        self.assertFalse(build_failed(SUCCESS))
        self.assertFalse(build_failed("dependency failed to start: container x exited (1)\n"))
