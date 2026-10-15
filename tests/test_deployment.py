"""Deployment contract and installer tests; no real sudo or service mutations."""
import configparser
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
UNITS = ('heimdall-bridge.service', 'heimdall-backup.service', 'heimdall-backup.timer')


def unit(name):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(ROOT / 'deploy' / name)
    return parser


class UnitTests(unittest.TestCase):
    def test_bridge_contract(self):
        service = unit(UNITS[0])
        values = service['Service']
        self.assertEqual(values['User'], 'haaland')
        self.assertEqual(values['Group'], 'haaland')
        self.assertEqual(values['WorkingDirectory'], str(ROOT))
        self.assertEqual(values['ExecStart'], f'{ROOT}/.venv/bin/python {ROOT}/scripts/run_bridge.py')
        self.assertEqual(values['Restart'], 'on-failure')
        self.assertEqual(values['KillMode'], 'control-group')
        self.assertEqual(values['MemoryHigh'], '1G')
        self.assertEqual(values['MemoryMax'], '1500M')
        self.assertEqual(service['Install']['WantedBy'], 'multi-user.target')

    def test_private_configuration_and_sandbox_compatibility(self):
        for name in UNITS[:2]:
            text = (ROOT / 'deploy' / name).read_text()
            self.assertIn('Environment=HOME=/home/haaland\n', text)
            self.assertIn('Environment=CODEX_HOME=/home/haaland/.codex\n', text)
            self.assertNotIn('EnvironmentFile', text)
            self.assertNotIn('TOKEN', text)
            self.assertNotIn('NoNewPrivileges', text)
            self.assertNotIn('RestrictNamespaces', text)
            self.assertEqual(unit(name)['Service']['UMask'], '0077')

    def test_daily_backup_is_separate(self):
        backup = unit(UNITS[1])
        self.assertEqual(backup['Service']['Type'], 'oneshot')
        self.assertEqual(backup['Service']['ExecStart'],
                         f'{ROOT}/.venv/bin/python {ROOT}/scripts/backup_bridge.py backup '
                         '--destination-dir /home/haaland/.local/share/heimdall/backups --include-assets')
        self.assertIn('/home/haaland/.local/bin', (ROOT / 'deploy' / UNITS[1]).read_text())
        self.assertNotIn('Install', backup)
        timer = unit(UNITS[2])
        self.assertEqual(timer['Timer']['OnCalendar'], '*-*-* 03:00:00')
        self.assertEqual(timer['Timer']['RandomizedDelaySec'], '1h')
        self.assertEqual(timer['Timer']['Persistent'], 'true')
        self.assertEqual(timer['Timer']['Unit'], UNITS[1])
        self.assertEqual(timer['Install']['WantedBy'], 'timers.target')

    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd-analyze unavailable')
    def test_real_unit_verification(self):
        result = subprocess.run(['systemd-analyze', 'verify', *[str(ROOT / 'deploy' / name) for name in UNITS]],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='heimdall-deployment-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.targets = self.root / 'units'
        self.bin = self.root / 'bin'
        self.backups = self.root / 'data/backups'
        self.backups.parent.mkdir()
        self.cgroups = self.root / 'cgroups'
        (self.cgroups / 'heimdall').mkdir(parents=True)
        (self.cgroups / 'heimdall/cgroup.freeze').touch()
        for path in (self.project / 'deploy', self.project / 'scripts', self.project / '.venv/bin', self.targets, self.bin):
            path.mkdir(parents=True)
        for name in UNITS:
            shutil.copyfile(ROOT / 'deploy' / name, self.project / 'deploy' / name)
        (self.project / '.venv/bin/python').symlink_to(shutil.which('python3'))
        for name in ('run_bridge.py', 'backup_bridge.py'):
            (self.project / 'scripts' / name).touch()
        self.db = self.root / 'bridge.sqlite3'
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('CREATE TABLE jobs(state TEXT)')
        self.log = self.root / 'calls'
        self.state = self.root / 'stopped'
        installer = (ROOT / 'scripts/install_service.sh').read_text()
        installer = installer.replace('project_dir=/home/haaland/heimdall', f'project_dir={self.project}')
        installer = installer.replace('unit_dir=/etc/systemd/system', f'unit_dir={self.targets}')
        installer = installer.replace('backup_dir=/home/haaland/.local/share/heimdall/backups', f'backup_dir={self.backups}')
        installer = installer.replace('cgroup_root=/sys/fs/cgroup', f'cgroup_root={self.cgroups}')
        self.installer = self.project / 'scripts/install_service.sh'
        self.installer.write_text(installer)
        self.stub('id', '#!/bin/bash\nif [[ $1 == -un ]]; then echo haaland; else echo 1000; fi\n')
        self.stub('systemd-analyze', '#!/bin/bash\necho "verify $*" >> "$TEST_LOG"\nexit "${VERIFY_FAIL:-0}"\n')
        self.stub('sudo', '''#!/bin/bash
echo "sudo $*" >> "$TEST_LOG"
# Reproduce the target: validation must use a noninteractive command, not -v.
[[ $1 == -v ]] && exit 99
if [[ $1 == -n ]]; then
 shift
 if [[ $1 == true ]]; then exit "${SUDO_FAIL:-0}"; fi
fi
if [[ $1 == install ]]; then
 cp -- "${@: -2:1}" "${@: -1}"
else
 exec "$@"
fi
''')
        self.stub('systemctl', '''#!/bin/bash
echo "ctl $*" >> "$TEST_LOG"
user=0
if [[ $1 == --user ]]; then user=1; shift; fi
if [[ $1 == show ]]; then
 case "$3" in
  --property=FragmentPath)
   if [[ $user == 1 ]]; then
    echo "${USER_FRAGMENT-/run/user/1000/systemd/transient/heimdall-bridge.service}"
   else
    echo "${SYSTEM_FRAGMENT-}"
   fi ;;
  --property=DropInPaths)
   if [[ $user == 1 ]]; then echo "${USER_DROPINS-}"; else echo "${DROPINS-}"; fi ;;
  --property=FreezerState)
   if [[ -e $TEST_FROZEN ]]; then echo "${FROZEN_STATE-frozen}"; else echo "${FREEZER_STATE-running}"; fi ;;
  --property=ControlGroup) echo /heimdall ;;
  --property=ActiveState)
   if [[ $user == 1 && ! -e $TEST_STOPPED ]]; then
    echo "${USER_STATE-active}"
   else
    echo "${SYSTEM_STATE-inactive}"
   fi ;;
 esac
elif [[ $1 == freeze ]]; then
 [[ ${FREEZE_FAIL:-0} == 0 ]] || exit 1
 touch "$TEST_FROZEN"
 if [[ ${INTERRUPT_FREEZE:-0} == 1 ]]; then kill -TERM "$PPID"; fi
 if [[ ${RACE:-0} == 1 ]]; then
  python3 -c 'import sqlite3,os; c=sqlite3.connect(os.environ["TEST_DB"]); c.execute("INSERT INTO jobs VALUES(?)", ("running",)); c.commit()'
 fi
elif [[ $1 == thaw ]]; then
 [[ ${THAW_FAIL:-0} == 0 ]] || exit 1
 rm -f -- "$TEST_FROZEN"
elif [[ $1 == stop ]]; then
 if [[ -e $TEST_FROZEN ]]; then
  echo 'Cannot perform operation on frozen unit heimdall-bridge.service.' >&2
  exit 1
 fi
 [[ ${STOP_FAIL:-0} == 0 ]] || exit 1
 if [[ -n ${LATE_JOB:-} ]]; then
  python3 -c 'import sqlite3,os; c=sqlite3.connect(os.environ["TEST_DB"]); c.execute("INSERT INTO jobs VALUES(?)", (os.environ["LATE_JOB"],)); c.commit(); c.close()'
 fi
 touch "$TEST_STOPPED"
fi
''')
        self.env = dict(os.environ, PATH=f'{self.bin}:/usr/bin:/bin', TEST_LOG=str(self.log),
                        TEST_STOPPED=str(self.state), TEST_DB=str(self.db), TEST_FROZEN=str(self.root / 'frozen'))

    def stub(self, name, contents):
        path = self.bin / name
        path.write_text(contents)
        path.chmod(0o755)

    def run_installer(self, *args, **env):
        return subprocess.run(['bash', str(self.installer), *args], env={**self.env, **env},
                              capture_output=True, text=True)

    def install(self, **env):
        return self.run_installer('--install', '--database', str(self.db), **env)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ''

    def assert_no_mutations(self):
        self.assertNotIn('sudo ', self.calls())
        self.assertNotIn('ctl --user freeze', self.calls())
        self.assertNotIn('ctl --user stop', self.calls())
        self.assertFalse(list(self.targets.iterdir()))
        self.assertFalse(self.backups.exists())

    def test_verify_only_and_default_never_mutate(self):
        for args in ((), ('--verify-only',)):
            result = self.run_installer(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_no_mutations()

    def test_verification_failure_precedes_mutations(self):
        result = self.install(VERIFY_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutations()

    def test_install_requires_explicit_database(self):
        self.assertNotEqual(self.run_installer('--install').returncode, 0)
        self.assert_no_mutations()

    def test_busy_jobs_fail_before_any_mutation(self):
        for state in ('queued', 'dispatching', 'running'):
            with self.subTest(state=state):
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute('DELETE FROM jobs')
                    conn.execute('INSERT INTO jobs VALUES(?)', (state,))
                result = self.install()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Handoff refused', result.stderr)
                self.assert_no_mutations()

    def test_missing_database_is_not_created(self):
        self.db.unlink()
        self.assertNotEqual(self.install().returncode, 0)
        self.assertFalse(self.db.exists())
        self.assert_no_mutations()

    def test_database_not_modified(self):
        original = self.db.read_bytes()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.db.read_bytes(), original)

    def test_conflicting_target_is_preserved(self):
        path = self.targets / UNITS[0]
        path.write_text('unrelated')
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_text(), 'unrelated')
        self.assertNotIn('sudo ', self.calls())

    def test_symlink_target_is_preserved(self):
        path = self.targets / UNITS[0]
        path.symlink_to(self.root / 'missing')
        self.assertNotEqual(self.install().returncode, 0)
        self.assertTrue(path.is_symlink())
        self.assertNotIn('sudo ', self.calls())

    def test_conflicting_managers_and_dropins(self):
        for env in ({'USER_FRAGMENT': '/unrelated/heimdall-bridge.service'},
                    {'SYSTEM_FRAGMENT': '/usr/lib/systemd/system/heimdall-bridge.service'},
                    {'DROPINS': '/etc/systemd/system/heimdall-bridge.service.d/override.conf'},
                    {'SYSTEM_STATE': 'active'}, {'USER_STATE': 'activating'}):
            with self.subTest(env=env):
                self.assertNotEqual(self.install(**env).returncode, 0)
                self.assert_no_mutations()

    def test_handoff_order_and_exact_scope(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertLess(calls.index('verify '), calls.index('sudo install'))
        self.assertIn('sudo -n true', calls)
        self.assertNotIn('sudo -v', calls)
        self.assertLess(calls.index('ctl --user freeze'), calls.index('ctl --user thaw'))
        self.assertLess(calls.index('ctl --user thaw'), calls.index('ctl --user stop'))
        lines = calls.splitlines()
        stop_index = lines.index('ctl --user stop heimdall-bridge.service')
        self.assertEqual(lines[stop_index - 1], 'ctl --user thaw heimdall-bridge.service')
        self.assertLess(calls.index('ctl --user stop'), calls.index('sudo systemctl start heimdall-bridge.service'))
        self.assertIn('sudo systemctl enable heimdall-bridge.service heimdall-backup.timer', calls)
        self.assertNotIn('start heimdall-backup.service', calls)
        self.assertNotIn('restart', calls)
        self.assertNotIn('kakao', calls.lower())
        self.assertEqual(sorted(p.name for p in self.targets.iterdir()), sorted(UNITS))

    def test_post_freeze_busy_job_thaws_without_stop_or_start(self):
        result = self.install(RACE='1')
        self.assertNotEqual(result.returncode, 0)
        calls = self.calls()
        self.assertIn('ctl --user thaw heimdall-bridge.service', calls)
        self.assertNotIn('ctl --user stop', calls)
        self.assertNotIn('sudo systemctl start', calls)

    def test_freeze_failure_never_stops_or_starts(self):
        self.assertNotEqual(self.install(FREEZE_FAIL='1').returncode, 0)
        self.assertIn('ctl --user thaw heimdall-bridge.service', self.calls())
        self.assertNotIn('ctl --user stop', self.calls())
        self.assertNotIn('sudo systemctl start', self.calls())

    def test_stop_failure_thaws_and_never_starts(self):
        self.assertNotEqual(self.install(STOP_FAIL='1').returncode, 0)
        self.assertIn('ctl --user thaw heimdall-bridge.service', self.calls())
        self.assertNotIn('sudo systemctl start', self.calls())

    def test_sudo_noninteractive_failure_precedes_mutations(self):
        self.assertNotEqual(self.install(SUDO_FAIL='1').returncode, 0)
        self.assertIn('sudo -n true', self.calls())
        self.assertNotIn('sudo install', self.calls())
        self.assertNotIn('sudo systemctl', self.calls())
        self.assertNotIn('ctl --user freeze', self.calls())
        self.assertFalse(self.backups.exists())
        self.assertFalse(list(self.targets.iterdir()))

    def test_thaw_failure_never_stops_or_starts(self):
        result = self.install(THAW_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('WARNING: thaw failed', result.stderr)
        self.assertNotIn('ctl --user stop', self.calls())
        self.assertNotIn('sudo systemctl start', self.calls())

    def test_job_appearing_during_thaw_stop_window_blocks_system_start(self):
        for state in ('queued', 'dispatching', 'running'):
            with self.subTest(state=state):
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute('DELETE FROM jobs')
                self.state.unlink(missing_ok=True)
                result = self.install(LATE_JOB=state)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Handoff refused', result.stderr)
                self.assertTrue(self.state.exists())
                self.assertNotIn('sudo systemctl enable', self.calls())
                self.assertNotIn('sudo systemctl start', self.calls())

    def test_uncertain_job_preserved_without_requeue(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO jobs VALUES('uncertain')")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT state FROM jobs').fetchall(), [('uncertain',)])

    def test_missing_backup_fails_before_mutations(self):
        (self.project / 'scripts/backup_bridge.py').unlink()
        self.assertNotEqual(self.install().returncode, 0)
        self.assert_no_mutations()

    def test_backup_directory_created_privately(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        info = self.backups.stat()
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
        self.assertEqual(info.st_uid, os.getuid())
        self.assertEqual(info.st_gid, os.getgid())

    def test_existing_private_backup_directory_preserved(self):
        self.backups.mkdir(mode=0o700)
        artifact = self.backups / 'existing-backup'
        artifact.write_text('preserved')
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(artifact.read_text(), 'preserved')

    def test_insecure_backup_directory_refused_without_chmod(self):
        self.backups.mkdir(mode=0o755)
        self.backups.chmod(0o755)
        self.assertNotEqual(self.install().returncode, 0)
        self.assertEqual(stat.S_IMODE(self.backups.stat().st_mode), 0o755)
        self.assertNotIn('sudo ', self.calls())

    def test_backup_symlink_refused_without_following(self):
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir(mode=0o700)
        self.backups.symlink_to(elsewhere)
        self.assertNotEqual(self.install().returncode, 0)
        self.assertTrue(self.backups.is_symlink())
        self.assertFalse(list(elsewhere.iterdir()))
        self.assertNotIn('sudo ', self.calls())

    def test_symlink_backup_parent_refused(self):
        self.backups.parent.rmdir()
        self.backups.parent.symlink_to(self.root / 'project')
        self.assertNotEqual(self.install().returncode, 0)
        self.assertFalse((self.project / 'backups').exists())
        self.assertNotIn('sudo ', self.calls())

    def test_unavailable_or_preexisting_freeze_fails_before_mutations(self):
        for state in ('', 'frozen', 'freezing', 'thawing'):
            with self.subTest(state=state):
                self.assertNotEqual(self.install(FREEZER_STATE=state).returncode, 0)
                self.assert_no_mutations()

    def test_missing_cgroup_freezer_fails_before_mutations(self):
        (self.cgroups / 'heimdall/cgroup.freeze').unlink()
        self.assertNotEqual(self.install().returncode, 0)
        self.assert_no_mutations()

    def test_unconfirmed_freeze_thaws_without_stop(self):
        self.assertNotEqual(self.install(FROZEN_STATE='freezing').returncode, 0)
        self.assertIn('ctl --user thaw heimdall-bridge.service', self.calls())
        self.assertNotIn('ctl --user stop', self.calls())
        self.assertNotIn('sudo systemctl start', self.calls())

    def test_interrupted_freeze_thaws_without_stop(self):
        result = self.install(INTERRUPT_FREEZE='1')
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertIn('ctl --user thaw heimdall-bridge.service', self.calls())
        self.assertNotIn('ctl --user stop', self.calls())
        self.assertNotIn('sudo systemctl start', self.calls())

    def test_user_dropins_fail_before_mutations(self):
        self.assertNotEqual(self.install(USER_DROPINS='/unexpected.conf').returncode, 0)
        self.assert_no_mutations()

    def test_identical_units_are_not_overwritten(self):
        for name in UNITS:
            shutil.copyfile(self.project / 'deploy' / name, self.targets / name)
        result = self.install(USER_STATE='inactive')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('sudo install', self.calls())
        self.assertNotIn('ctl --user freeze', self.calls())
        self.assertNotIn('ctl --user stop', self.calls())


if __name__ == '__main__':
    unittest.main()
