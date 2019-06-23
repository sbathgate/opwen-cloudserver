from collections import defaultdict
from copy import deepcopy
from unittest import TestCase
from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import patch
from uuid import uuid4

from libcloud.storage.types import ObjectDoesNotExistError

from opwen_email_server import actions
from opwen_email_server.constants import sync
from opwen_email_server.services.storage import AccessInfo
from opwen_email_server.utils.serialization import from_jsonl_bytes
from opwen_email_server.utils.serialization import to_jsonl_bytes


class ActionTests(TestCase):
    @patch.object(actions._Action, '_telemetry_client')
    @patch.object(actions._Action, '_telemetry_channel')
    def test_logs_exception(self, mock_channel, mock_client):
        class TestAction(actions._Action):
            def _action(self):
                int('not-a-number')

        with self.assertRaises(ValueError):
            action = TestAction()
            action()

        mock_client.track_exception.assert_called_once_with()
        mock_channel.flush.assert_called_once_with()


class PingTests(TestCase):
    def test_200(self):
        action = actions.Ping()
        message, status = action()
        self.assertEqual(status, 200)


# noinspection PyTypeChecker
class SendOutboundEmailsTests(TestCase):
    def setUp(self):
        self.email_storage = Mock()
        self.send_email = MagicMock()

    def test_200(self):
        resource_id = str(uuid4())
        email = {'subject': 'test'}

        self.email_storage.fetch_object.return_value = email
        self.send_email.return_value = True

        _, status = self._execute_action(resource_id)

        self.assertEqual(status, 200)
        self.email_storage.fetch_object.assert_called_once_with(resource_id)
        self.send_email.assert_called_once_with(email)

    def test_500(self):
        resource_id = str(uuid4())
        email = {'subject': 'test'}

        self.email_storage.fetch_object.return_value = email
        self.send_email.return_value = False

        _, status = self._execute_action(resource_id)

        self.assertEqual(status, 500)
        self.email_storage.fetch_object.assert_called_once_with(resource_id)
        self.send_email.assert_called_once_with(email)

    def _execute_action(self, *args, **kwargs):
        action = actions.SendOutboundEmails(
            email_storage=self.email_storage,
            send_email=self.send_email)

        return action(*args, **kwargs)


# noinspection PyTypeChecker
class StoreInboundEmailsTests(TestCase):
    def setUp(self):
        self.raw_email_storage = Mock()
        self.email_storage = Mock()
        self.pending_storage = Mock()
        self.pending_factory = MagicMock()
        self.email_parser = MagicMock()

    def test_202(self):
        # noinspection PyUnusedLocal
        def throw(*args, **kwargs):
            raise ObjectDoesNotExistError(None, None, None)

        resource_id = str(uuid4())

        self.raw_email_storage.fetch_text.side_effect = throw

        _, status = self._execute_action(resource_id)

        self.assertEqual(status, 202)
        self.raw_email_storage.fetch_text.assert_called_once_with(resource_id)
        self.assertFalse(self.raw_email_storage.delete.called)
        self.assertFalse(self.email_storage.store_object.called)
        self.assertFalse(self.pending_factory.called)
        self.assertFalse(self.pending_storage.store_text.called)
        self.assertFalse(self.email_parser.called)

    def test_200(self):
        resource_id = 'b8dcaf40-fd14-4a89-8898-c9514b0ad724'
        domain = 'test.com'
        raw_email = 'dummy-mime'
        parsed_email = {'to': ['foo@{}'.format(domain)]}
        email_id = 'eaee7d51a5440981d5df66825134dcfe0490f145e1ad689bce8c68caeaecde53'
        stored_email = dict(parsed_email)
        stored_email['_uid'] = email_id

        self.raw_email_storage.fetch_text.return_value = raw_email
        self.pending_factory.return_value = self.pending_storage
        self.email_parser.return_value = parsed_email

        _, status = self._execute_action(resource_id)

        self.assertEqual(status, 200)
        self.raw_email_storage.fetch_text.assert_called_once_with(resource_id)
        self.raw_email_storage.delete.assert_called_once_with(resource_id)
        self.email_storage.store_object.assert_called_once_with(email_id, stored_email)
        self.pending_factory.assert_called_once_with(domain)
        self.pending_storage.store_text.assert_called_once_with(email_id, 'pending')
        self.email_parser.assert_called_once_with(raw_email)

    def _execute_action(self, *args, **kwargs):
        action = actions.StoreInboundEmails(
            raw_email_storage=self.raw_email_storage,
            email_storage=self.email_storage,
            pending_factory=self.pending_factory,
            email_parser=self.email_parser)

        return action(*args, **kwargs)


# noinspection PyTypeChecker
class StoreWrittenClientEmailsTests(TestCase):
    def setUp(self):
        self.client_storage = Mock()
        self.email_storage = Mock()
        self.next_task = MagicMock()

    def test_200(self):
        attachment_content_bytes = b'some file content'
        attachment_content_base64 = 'c29tZSBmaWxlIGNvbnRlbnQ='
        resource_id = str(uuid4())
        email_id = str(uuid4())

        client_email = {'from': 'foo@test.com', '_uid': email_id, 'attachments': [
            {'filename': 'test.txt', 'content': attachment_content_base64}
        ]}
        server_email = deepcopy(client_email)
        server_email['attachments'][0]['content'] = attachment_content_bytes

        self.client_storage.fetch_objects.return_value = [client_email]

        _, status = self._execute_action(resource_id)

        self.assertEqual(status, 200)
        self.client_storage.fetch_objects.assert_called_once_with(resource_id, (sync.EMAILS_FILE, from_jsonl_bytes))
        self.email_storage.store_object.assert_called_once_with(email_id, server_email)
        self.next_task.assert_called_once_with(email_id)
        self.client_storage.delete.assert_called_once_with(resource_id)

    def _execute_action(self, *args, **kwargs):
        action = actions.StoreWrittenClientEmails(
            client_storage=self.client_storage,
            email_storage=self.email_storage,
            next_task=self.next_task)

        return action(*args, **kwargs)


# noinspection PyTypeChecker
class ReceiveInboundEmailTests(TestCase):
    def setUp(self):
        self.auth = Mock()
        self.raw_email_storage = Mock()
        self.next_task = MagicMock()
        self.email_id_source = MagicMock()

    def test_403(self):
        client_id = str(uuid4())
        domain = None
        email = 'dummy-mime'

        self.auth.domain_for.return_value = domain

        _, status = self._execute_action(client_id, email)

        self.assertEqual(status, 403)
        self.auth.domain_for.assert_called_once_with(client_id)

    def test_200(self):
        client_id = str(uuid4())
        email_id = 'dfbab492b9adcf20ca8424b993b0f7ec26731d069be4d451ebbf7910937a999c'
        domain = 'test.com'
        email = 'dummy-mime'

        self.auth.domain_for.return_value = domain

        _, status = self._execute_action(client_id, email)

        self.assertEqual(status, 200)
        self.auth.domain_for.assert_called_once_with(client_id)
        self.raw_email_storage.store_text.assert_called_once_with(email_id, email)
        self.next_task.assert_called_once_with(email_id)

    def test_is_idempotent(self):
        client_id = str(uuid4())
        domain = 'test.com'
        email = 'dummy-mime'
        num_repeated_emails = 3

        self.auth.domain_for.return_value = domain

        for _ in range(num_repeated_emails):
            _, status = self._execute_action(client_id, email)
            self.assertEqual(status, 200)

        self.assertHasSameCalls(self.raw_email_storage.store_text, num_repeated_emails)
        self.assertHasSameCalls(self.next_task, num_repeated_emails)

    def assertHasSameCalls(self, mocked_function, num_calls):
        self.assertEqual(len(mocked_function.call_args_list), num_calls)
        for call in mocked_function.call_args_list:
            self.assertEqual(call, mocked_function.call_args_list[0])

    def _execute_action(self, *args, **kwargs):
        action = actions.ReceiveInboundEmail(
            auth=self.auth,
            raw_email_storage=self.raw_email_storage,
            next_task=self.next_task)

        return action(*args, **kwargs)


# noinspection PyTypeChecker
class DownloadClientEmailsTests(TestCase):
    def setUp(self):
        self.auth = Mock()
        self.client_storage = Mock()
        self.email_storage = Mock()
        self.pending_factory = MagicMock()
        self.pending_storage = Mock()

    def test_400(self):
        client_id = str(uuid4())
        domain = 'test.com'

        self.auth.domain_for.return_value = domain
        self.client_storage.compression_formats.return_value = ['gz']

        _, status = self._execute_action(client_id, 'xyz')

        self.assertEqual(status, 400)

    def test_403(self):
        client_id = str(uuid4())
        domain = None

        self.auth.domain_for.return_value = domain

        _, status = self._execute_action(client_id, 'gz')

        self.assertEqual(status, 403)

    def test_200(self):
        attachment_content_bytes = b'some file content'
        attachment_content_base64 = 'c29tZSBmaWxlIGNvbnRlbnQ='
        client_id = str(uuid4())
        email_id = str(uuid4())
        resource_id = str(uuid4())
        domain = 'test.com'

        server_email = {'_uid': email_id, 'attachments': [
            {'filename': 'test.txt', 'content': attachment_content_bytes}
        ]}
        client_email = deepcopy(server_email)
        client_email['attachments'][0]['content'] = attachment_content_base64

        _stored = defaultdict(list)
        _compression = defaultdict(list)
        _serializers = defaultdict(list)

        def store_objects_mock(upload, compression):
            name, emails, serializer = upload
            _stored[name].extend(emails)
            _compression[name].append(compression)
            _serializers[name].append(serializer)
            return resource_id

        self.auth.domain_for.return_value = domain
        self.pending_factory.return_value = self.pending_storage
        self.pending_storage.iter.return_value = [email_id]
        self.email_storage.fetch_object.return_value = server_email
        self.client_storage.store_objects.side_effect = store_objects_mock
        self.client_storage.compression_formats.return_value = ['gz']

        response = self._execute_action(client_id, 'gz')

        self.assertEqual(response.get('resource_id'), resource_id)
        self.auth.domain_for.assert_called_once_with(client_id)
        self.pending_factory.assert_called_once_with(domain)
        self.pending_storage.iter.assert_called_once_with()
        self.pending_storage.delete.assert_called_once_with(email_id)
        self.email_storage.fetch_object.assert_called_once_with(email_id)
        self.assertEqual(_stored[sync.EMAILS_FILE], [client_email])
        self.assertEqual(_compression[sync.EMAILS_FILE], ['gz'])
        self.assertEqual(_serializers[sync.EMAILS_FILE], [to_jsonl_bytes])

    def _execute_action(self, *args, **kwargs):
        action = actions.DownloadClientEmails(
            auth=self.auth,
            client_storage=self.client_storage,
            email_storage=self.email_storage,
            pending_factory=self.pending_factory)

        return action(*args, **kwargs)


# noinspection PyTypeChecker
class UploadClientEmailsTests(TestCase):
    def setUp(self):
        self.auth = Mock()
        self.next_task = MagicMock()

    def test_403(self):
        client_id = str(uuid4())
        domain = None
        upload_info = {}

        self.auth.domain_for.return_value = domain

        _, status = self._execute_action(client_id, upload_info)

        self.assertEqual(status, 403)
        self.auth.domain_for.assert_called_once_with(client_id)

    def test_200(self):
        client_id = str(uuid4())
        resource_id = str(uuid4())
        domain = 'test.com'
        upload_info = {'resource_id': resource_id}

        self.auth.domain_for.return_value = domain

        _, status = self._execute_action(client_id, upload_info)

        self.assertEqual(status, 200)
        self.auth.domain_for.assert_called_once_with(client_id)
        self.next_task.assert_called_once_with(resource_id)

    def _execute_action(self, *args, **kwargs):
        action = actions.UploadClientEmails(
            auth=self.auth,
            next_task=self.next_task)

        return action(*args, **kwargs)


# noinspection PyTypeChecker
class RegisterClientTests(TestCase):
    def setUp(self):
        self.auth = Mock()
        self.client_storage = Mock()
        self.setup_mailbox = MagicMock()
        self.setup_mx_records = MagicMock()
        self.client_id_source = MagicMock()

    def test_400(self):
        domain = 'TEST.com'

        _, status = self._execute_action({'domain': domain})

        self.assertEqual(status, 400)

    def test_409(self):
        domain = 'test.com'

        self.auth.client_id_for.return_value = '123'

        _, status = self._execute_action({'domain': domain})

        self.assertEqual(status, 409)
        self.auth.client_id_for.assert_called_once_with(domain)

    def test_200(self):
        client_id = str(uuid4())
        client_storage_account = 'account'
        client_storage_key = 'key'
        client_storage_container = 'container'
        domain = 'test.com'

        self.client_id_source.return_value = client_id
        self.auth.client_id_for.return_value = None
        self.client_storage.access_info.return_value = AccessInfo(
            account=client_storage_account,
            key=client_storage_key,
            container=client_storage_container)

        response = self._execute_action({'domain': domain})

        self.assertEqual(response['client_id'], client_id)
        self.assertEqual(response['storage_account'], client_storage_account)
        self.assertEqual(response['storage_key'], client_storage_key)
        self.assertEqual(response['resource_container'], client_storage_container)
        self.auth.client_id_for.assert_called_once_with(domain)
        self.auth.insert.assert_called_with(client_id, domain)
        self.assertEqual(self.client_storage.ensure_exists.call_count, 1)
        self.setup_mailbox.assert_called_once_with(client_id, domain)
        self.setup_mx_records.assert_called_once_with(domain)

    def _execute_action(self, *args, **kwargs):
        action = actions.RegisterClient(
            auth=self.auth,
            client_storage=self.client_storage,
            setup_mailbox=self.setup_mailbox,
            setup_mx_records=self.setup_mx_records,
            client_id_source=self.client_id_source)

        return action(*args, **kwargs)


class CalculatePendingEmailsMetricTests(TestCase):
    def setUp(self):
        self.auth = Mock()
        self.pending_storage = Mock()
        self.pending_factory = MagicMock()

    def test_404(self):
        client_domain = 'does.not.exist'
        client_id = None

        self.auth.client_id_for.return_value = client_id

        _, status = self._execute_action(client_domain)

        self.assertEqual(status, 404)
        self.auth.client_id_for.assert_called_once_with(client_domain)

    def test_200(self):
        client_domain = 'test.com'
        client_id = str(uuid4())
        pending_email_ids = [str(uuid4()), str(uuid4()), str(uuid4())]

        self.auth.client_id_for.return_value = client_id
        self.pending_factory.return_value = self.pending_storage
        self.pending_storage.iter.return_value = pending_email_ids

        response = self._execute_action(client_domain)

        self.assertEqual(response['pending_emails'], len(pending_email_ids))
        self.auth.client_id_for.assert_called_once_with(client_domain)
        self.pending_factory.assert_called_once_with(client_domain)
        self.pending_storage.iter.assert_called_once_with()

    def _execute_action(self, *args, **kwargs):
        action = actions.CalculatePendingEmailsMetric(
            auth=self.auth,
            pending_factory=self.pending_factory)

        return action(*args, **kwargs)
