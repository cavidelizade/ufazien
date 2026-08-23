"""
The rules that keep one person's site out of another person's.

Each test here stands for something that was actually reachable, so a
regression in any of them is a way back in rather than a style problem.
"""

import os
import tempfile

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from . import domains
from .models import Database, Domain, SubscriptionPlan, Website
from .views import path_within

User = get_user_model()


class PathContainmentTests(TestCase):
    """
    `delete_file` and `download_file` compared strings:
    `file_path.startswith(website_dir)`. `/srv/hosting/alice` starts with
    `/srv/hosting/a`, and people choose their own subdomains — so a site called
    `a` could read and delete files in every site whose name began with an `a`.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        for site in ('a', 'alice', 'andrew'):
            os.makedirs(os.path.join(self.root, site), exist_ok=True)
            with open(os.path.join(self.root, site, '.env'), 'w') as handle:
                handle.write(f'SECRET={site}\n')
        self.site = os.path.join(self.root, 'a')

    def test_a_file_of_your_own_resolves(self):
        self.assertEqual(
            path_within(self.site, 'index.html'),
            os.path.join(os.path.realpath(self.site), 'index.html'),
        )

    def test_a_neighbour_sharing_your_prefix_does_not(self):
        for neighbour in ('../alice/.env', '../andrew/.env'):
            self.assertIsNone(path_within(self.site, neighbour), neighbour)

    def test_climbing_out_altogether_does_not(self):
        self.assertIsNone(path_within(self.site, '../../../../etc/passwd'))

    def test_an_absolute_path_does_not(self):
        self.assertIsNone(path_within(self.site, '/etc/passwd'))

    def test_a_symlink_pointing_out_does_not(self):
        """`normpath` cannot see through a link; `realpath` can."""
        link = os.path.join(self.site, 'escape')
        os.symlink(os.path.join(self.root, 'alice'), link)

        self.assertIsNone(path_within(self.site, 'escape/.env'))

    def test_a_harmless_dot_dot_inside_the_site_is_fine(self):
        self.assertEqual(
            path_within(self.site, 'css/../index.html'),
            os.path.join(os.path.realpath(self.site), 'index.html'),
        )


class SubdomainRuleTests(TestCase):
    def test_a_reserved_name_is_refused(self):
        for name in ('api', 'www', 'admin', 'login', 'logs', 'postgres'):
            with self.assertRaises(ValueError, msg=name):
                domains.check(f'{name}.ufazien.com')

    def test_an_ordinary_name_is_allowed(self):
        self.assertEqual(domains.check('alice.ufazien.com'), 'alice.ufazien.com')

    def test_a_name_is_stored_in_one_case(self):
        """
        Stored as typed, `Alice` and `alice` were two rows resolving to one
        nginx directory — one of which nobody could ever serve.
        """
        self.assertEqual(domains.check('Alice.UFAZIEN.com'), 'alice.ufazien.com')

    def test_something_that_is_not_a_hostname_is_refused(self):
        for name in ('my site.ufazien.com', '-bad.ufazien.com', 'bad-.ufazien.com'):
            with self.assertRaises(ValueError, msg=name):
                domains.check(name)

    def test_a_domain_of_your_own_is_not_policed(self):
        self.assertEqual(domains.check('example.com'), 'example.com')


class DomainClaimTests(TestCase):
    """A name somebody else holds should be a 400, not a 500."""

    def setUp(self):
        self.owner = User.objects.create_user(username='owner', email='o@e.com', password='pw')
        self.other = User.objects.create_user(username='other', email='x@e.com', password='pw')
        Domain.objects.create(name='taken.ufazien.com', domain_type='subdomain', user=self.owner)

        # Without a plan the view refuses every name with "Free subscription
        # plan not found" — a 400 that has nothing to do with the domain. A
        # test asserting only on the status code passes with the domain rules
        # deleted, which is how this one was first written.
        SubscriptionPlan.objects.create(
            name='free', display_name='Free', price=0, max_websites=3,
            max_databases=1, storage_limit_mb=100, bandwidth_limit_mb=1000,
        )
        self.api = APIClient()
        self.api.force_authenticate(user=self.other)

    def create(self, name):
        return self.api.post('/api/hosting/websites/', {
            'name': 'A site', 'website_type': 'static', 'new_domain_name': name,
        }, format='json')

    def complaint(self, response):
        return ' '.join(response.json().get('new_domain_name', []))

    def test_claiming_a_name_somebody_else_has_is_refused_cleanly(self):
        """
        The check was scoped to the requester, so a name another account held
        passed validation and hit the unique index — a 500 with a traceback.
        """
        response = self.create('taken.ufazien.com')

        self.assertEqual(response.status_code, 400, response.content[:200])
        self.assertIn('already taken', self.complaint(response))

    def test_a_reserved_name_is_refused_cleanly(self):
        response = self.create('admin.ufazien.com')

        self.assertEqual(response.status_code, 400, response.content[:200])
        self.assertIn('reserved', self.complaint(response))
        self.assertFalse(Domain.objects.filter(name='admin.ufazien.com').exists())

    def test_a_name_nobody_holds_is_allowed(self):
        """Otherwise the tests above would pass with everything refused."""
        response = self.create('quite-free.ufazien.com')

        self.assertEqual(response.status_code, 201, response.content[:300])
        self.assertTrue(
            Domain.objects.filter(name='quite-free.ufazien.com', user=self.other).exists()
        )

    def test_case_does_not_let_you_take_a_name_twice(self):
        """`Taken` and `taken` resolve to one nginx directory."""
        response = self.create('TAKEN.ufazien.com')

        self.assertEqual(response.status_code, 400, response.content[:200])
        self.assertIn('already taken', self.complaint(response))


class DatabaseCredentialTests(TestCase):
    """
    The browser used to generate the username and password with
    `Math.random()` and post them, and what it sent became the real credential
    on the real database.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='dbuser', email='db@e.com', password='pw')
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def test_the_client_cannot_choose_the_password(self):
        from .serializers import DatabaseSerializer

        serializer = DatabaseSerializer(data={
            'name': 'mydb', 'db_type': 'mysql',
            'username': 'chosen_by_me', 'password': 'chosen-by-me',
        })
        serializer.is_valid()

        self.assertNotIn('password', serializer.validated_data)
        self.assertNotIn('username', serializer.validated_data)

    def test_the_owner_can_still_read_their_own_credentials(self):
        """They need them to connect, and the queryset is scoped to them."""
        from .serializers import DatabaseSerializer

        database = Database.objects.create(
            user=self.user, name='mydb', db_type='mysql',
            username='user_abc', password='server-minted',
        )

        self.assertEqual(DatabaseSerializer(database).data['password'], 'server-minted')

    def test_the_server_generated_password_is_not_predictable(self):
        from .tasks import generate_password

        minted = {generate_password() for _ in range(200)}

        self.assertEqual(len(minted), 200)
        self.assertTrue(all(len(p) >= 32 for p in minted))


class LoginThrottleTests(TestCase):
    """Signing in had no limit at all, so a password could be guessed as fast
    as the network allowed."""

    def setUp(self):
        cache.clear()
        User.objects.create_user(username='victim', email='victim@e.com', password='the-real-one')
        self.api = APIClient()

    def tearDown(self):
        cache.clear()

    def attempt(self, password, address='10.0.0.1'):
        return self.api.post(
            '/api/auth/login/', {'email': 'victim@e.com', 'password': password},
            format='json', REMOTE_ADDR=address,
        ).status_code

    def configured_limit(self):
        """
        Read the rate the server actually runs on, rather than overriding it.

        `SimpleRateThrottle.THROTTLE_RATES` is bound to the settings dict when
        the class is imported, so `override_settings` never reaches it: a test
        that sets a low rate and sends a few requests sees every one of them
        succeed, which is indistinguishable from no throttle at all.
        """
        from rest_framework.throttling import ScopedRateThrottle

        throttle = ScopedRateThrottle()
        count, _ = throttle.parse_rate(throttle.THROTTLE_RATES['login'])
        return count

    def test_guessing_is_cut_off(self):
        limit = self.configured_limit()

        statuses = [self.attempt(f'guess{i}') for i in range(limit + 2)]

        self.assertEqual(statuses[-1], 429, f'never throttled in {len(statuses)}: {statuses}')

    def test_a_few_mistakes_are_not_punished(self):
        """Somebody mistyping their own password should not be locked out."""
        statuses = [self.attempt('oops') for _ in range(3)]

        self.assertNotIn(429, statuses)

    def test_the_real_password_still_works_before_the_limit(self):
        self.assertEqual(self.attempt('the-real-one'), 200)

    def test_signing_up_is_limited_too(self):
        """Otherwise the account table is a free-for-all."""
        from rest_framework.throttling import ScopedRateThrottle

        self.assertIn('signup', ScopedRateThrottle().THROTTLE_RATES)


class SecretKeyGuardTests(TestCase):
    """
    The fallback key is published in this repository, and everything Django
    signs comes from it — including the JWTs the API authenticates with, so
    anybody who can read the source could mint a token for any account.

    The guard runs while `settings.py` is being read, which is long before a
    test can call it. So this starts a real interpreter with an environment and
    reads what Django does about it.
    """

    def django_starts(self, **environment):
        """Import the settings in a subprocess; True if Django came up."""
        import subprocess
        import sys

        env = dict(os.environ, DJANGO_SETTINGS_MODULE='ufazien.settings')
        env.pop('SECRET_KEY', None)
        env.update(environment)

        finished = subprocess.run(
            [sys.executable, '-c', 'import django; django.setup()'],
            capture_output=True, text=True, env=env,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        return finished.returncode == 0, finished.stderr

    def test_production_refuses_to_start_on_the_published_fallback(self):
        started, stderr = self.django_starts(DJANGO_DEBUG='False')

        self.assertFalse(started, 'production started on the fallback key')
        self.assertIn('SECRET_KEY', stderr)

    def test_production_starts_once_a_key_is_set(self):
        started, stderr = self.django_starts(
            DJANGO_DEBUG='False', SECRET_KEY='a-real-key-set-in-the-environment',
        )

        self.assertTrue(started, stderr[-600:])

    def test_local_development_is_unaffected(self):
        """Nobody should need to invent a key to run the tests or the server."""
        started, stderr = self.django_starts(DJANGO_DEBUG='True')

        self.assertTrue(started, stderr[-600:])


class LogPrivacyTests(TestCase):
    """
    Signing in wrote the address to stdout — `[LOGIN] Login successful for
    user id=1, email=victim@e.com`. Application logs are shipped, searched and
    kept far longer than anything else, and an address identifies a person.

    The same rule as `community/serializers.py`: an identifier belongs to the
    person it names. A user id says everything an operator needs.
    """

    def setUp(self):
        cache.clear()
        self.address = 'quiet@example.com'
        User.objects.create_user(username='quiet', email=self.address, password='pw-correct')
        self.api = APIClient()

    def tearDown(self):
        cache.clear()

    def login(self, password):
        with self.assertLogs(level='DEBUG') as captured:
            self.api.post('/api/auth/login/',
                          {'email': self.address, 'password': password}, format='json')
        return '\n'.join(captured.output)

    def test_a_successful_login_does_not_log_the_address(self):
        self.assertNotIn(self.address, self.login('pw-correct'))

    def test_a_failed_login_does_not_log_the_address(self):
        self.assertNotIn(self.address, self.login('wrong'))

    def test_a_login_for_no_account_does_not_log_the_address(self):
        with self.assertLogs(level='DEBUG') as captured:
            self.api.post('/api/auth/login/',
                          {'email': 'nobody@example.com', 'password': 'x'}, format='json')

        self.assertNotIn('nobody@example.com', '\n'.join(captured.output))

    def test_signing_up_does_not_log_the_address(self):
        with self.assertLogs(level='DEBUG') as captured:
            self.api.post('/api/auth/signup/', {
                'username': 'newcomer', 'email': 'newcomer@example.com',
                'password': 'a-long-enough-password',
                'first_name': 'New', 'last_name': 'Comer',
            }, format='json')

        self.assertNotIn('newcomer@example.com', '\n'.join(captured.output))

    def test_the_login_view_no_longer_prints(self):
        """
        `print()` bypasses log configuration entirely, so nothing can filter
        it — and it crashes on Windows under cp1252 when the message carries
        an emoji, which is why CLAUDE.md asks for logging.
        """
        import inspect

        from users import views

        source = inspect.getsource(views.LoginView)

        self.assertNotIn('print(', source)


class PasswordRotationTests(TestCase):
    """
    `change_password` assigned `database.password` and saved. Nothing ever
    reached the database server, so the dashboard showed a new password while
    the real role kept the old one — somebody rotating a credential they
    believed had leaked ended up with the leaked one still live.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='dbo', email='dbo@e.com', password='pw')
        self.database = Database.objects.create(
            user=self.user, name='mydb', db_type='mysql',
            username='user_abc', password='the-old-one', status='active',
        )
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def rotate(self, password):
        return self.api.post(
            f'/api/hosting/databases/{self.database.id}/change_password/',
            {'password': password}, format='json',
        )

    def test_a_rotation_reaches_the_database_server(self):
        from unittest.mock import patch

        with patch('hosting.views.set_database_password') as task:
            response = self.rotate('a-new-strong-password')

        self.assertEqual(response.status_code, 200, response.content[:200])
        task.delay.assert_called_once_with(str(self.database.id), 'a-new-strong-password')

    def test_a_server_that_refuses_is_reported_rather_than_claimed(self):
        """
        Celery runs eagerly here, so a failure propagates. Answering 200 would
        tell somebody their leaked password had been replaced when it had not.
        """
        from unittest.mock import patch

        with patch('hosting.views.set_database_password') as task:
            task.delay.side_effect = OSError('could not connect')
            response = self.rotate('a-new-strong-password')

        self.assertEqual(response.status_code, 502)
        self.database.refresh_from_db()
        self.assertEqual(self.database.password, 'the-old-one')

    def test_the_stored_row_is_not_written_by_the_view(self):
        """The task writes it, and only after the server has accepted it."""
        from unittest.mock import patch

        with patch('hosting.views.set_database_password'):
            self.rotate('a-new-strong-password')

        self.database.refresh_from_db()
        self.assertEqual(self.database.password, 'the-old-one')

    def test_a_short_password_is_still_refused(self):
        response = self.rotate('short')

        self.assertEqual(response.status_code, 400)

    def test_somebody_elses_database_is_not_rotatable(self):
        """`get_queryset` scopes to the owner, so this must 404, not 403."""
        intruder = User.objects.create_user(username='nosy', email='n@e.com', password='pw')
        api = APIClient()
        api.force_authenticate(user=intruder)

        response = api.post(
            f'/api/hosting/databases/{self.database.id}/change_password/',
            {'password': 'a-new-strong-password'}, format='json',
        )

        self.assertEqual(response.status_code, 404)

    def test_the_task_sends_the_password_as_a_parameter(self):
        """
        Interpolating it into the SQL would let a password containing a quote
        end the statement. The role name cannot be a parameter, so it goes
        through the driver's identifier quoting instead.
        """
        import inspect

        from . import tasks

        source = inspect.getsource(tasks.set_database_password)

        self.assertNotIn('%s" % ', source)
        self.assertIn('%s', source)
        self.assertIn('sql.Identifier', source)


class TaskErrorDisclosureTests(TestCase):
    """
    `error_message` is serialised to the database's owner. A driver's
    connection failure names the admin host, port and user it tried — the
    platform's infrastructure, which is not the owner's to see.
    """

    def test_a_failure_does_not_hand_the_owner_the_admin_connection(self):
        from unittest.mock import patch

        from .tasks import set_database_password

        user = User.objects.create_user(username='t', email='t@e.com', password='pw')
        database = Database.objects.create(
            user=user, name='db', db_type='postgresql',
            username='user_abc', password='old', status='active',
        )
        leaky = OSError(
            'connection to server at "postgres.ufazien.com", port 5433 failed: '
            'FATAL: password authentication failed for user "hosting_admin"'
        )

        with patch('hosting.tasks._import_psycopg2', side_effect=leaky):
            with self.assertRaises(Exception):
                set_database_password(str(database.id), 'a-new-password')

        database.refresh_from_db()
        for secret in ('postgres.ufazien.com', '5433', 'hosting_admin'):
            self.assertNotIn(secret, database.error_message, secret)
