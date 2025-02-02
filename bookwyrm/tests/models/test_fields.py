""" testing models """
from io import BytesIO
from collections import namedtuple
from dataclasses import dataclass
import json
import pathlib
import re
from typing import List
from unittest.mock import patch

from PIL import Image
import responses

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import models
from django.test import TestCase
from django.utils import timezone

from bookwyrm import activitypub
from bookwyrm.activitypub.base_activity import ActivityObject
from bookwyrm.models import fields, User, Status
from bookwyrm.models.base_model import BookWyrmModel
from bookwyrm.models.activitypub_mixin import ActivitypubMixin

# pylint: disable=too-many-public-methods
@patch("bookwyrm.suggested_users.rerank_suggestions_task.delay")
class ModelFields(TestCase):
    """overwrites standard model feilds to work with activitypub"""

    def test_validate_remote_id(self, _):
        """should look like a url"""
        self.assertIsNone(fields.validate_remote_id("http://www.example.com"))
        self.assertIsNone(fields.validate_remote_id("https://www.example.com"))
        self.assertIsNone(fields.validate_remote_id("http://exle.com/dlg-23/x"))
        self.assertRaises(
            ValidationError, fields.validate_remote_id, "http:/example.com/dlfjg-23/x"
        )
        self.assertRaises(
            ValidationError, fields.validate_remote_id, "www.example.com/dlfjg-23/x"
        )
        self.assertRaises(
            ValidationError,
            fields.validate_remote_id,
            "http://www.example.com/dlfjg 23/x",
        )

    def test_activitypub_field_mixin(self, _):
        """generic mixin with super basic to and from functionality"""
        instance = fields.ActivitypubFieldMixin()
        self.assertEqual(instance.field_to_activity("fish"), "fish")
        self.assertEqual(instance.field_from_activity("fish"), "fish")
        self.assertFalse(instance.deduplication_field)

        instance = fields.ActivitypubFieldMixin(
            activitypub_wrapper="endpoints", activitypub_field="outbox"
        )
        self.assertEqual(instance.field_to_activity("fish"), {"outbox": "fish"})
        self.assertEqual(instance.field_from_activity({"outbox": "fish"}), "fish")
        self.assertEqual(instance.get_activitypub_field(), "endpoints")

        instance = fields.ActivitypubFieldMixin()
        instance.name = "snake_case_name"
        self.assertEqual(instance.get_activitypub_field(), "snakeCaseName")

    def test_set_field_from_activity(self, _):
        """setter from entire json blob"""

        @dataclass
        class TestModel:
            """real simple mock"""

            field_name: str

        mock_model = TestModel(field_name="bip")
        TestActivity = namedtuple("test", ("fieldName", "unrelated"))
        data = TestActivity(fieldName="hi", unrelated="bfkjh")

        instance = fields.ActivitypubFieldMixin()
        instance.name = "field_name"

        instance.set_field_from_activity(mock_model, data)
        self.assertEqual(mock_model.field_name, "hi")

    def test_set_activity_from_field(self, _):
        """set json field given entire model"""

        @dataclass
        class TestModel:
            """real simple mock"""

            field_name: str
            unrelated: str

        mock_model = TestModel(field_name="bip", unrelated="field")
        instance = fields.ActivitypubFieldMixin()
        instance.name = "field_name"

        data = {}
        instance.set_activity_from_field(data, mock_model)
        self.assertEqual(data["fieldName"], "bip")

    def test_remote_id_field(self, _):
        """just sets some defaults on charfield"""
        instance = fields.RemoteIdField()
        self.assertEqual(instance.max_length, 255)
        self.assertTrue(instance.deduplication_field)

        with self.assertRaises(ValidationError):
            instance.run_validators("http://www.example.com/dlfjg 23/x")

    def test_username_field(self, _):
        """again, just setting defaults on username field"""
        instance = fields.UsernameField()
        self.assertEqual(instance.activitypub_field, "preferredUsername")
        self.assertEqual(instance.max_length, 150)
        self.assertEqual(instance.unique, True)
        with self.assertRaises(ValidationError):
            instance.run_validators("mouse")
            instance.run_validators("mouseexample.com")
            instance.run_validators("mouse@example.c")
            instance.run_validators("@example.com")
            instance.run_validators("mouse@examplecom")
            instance.run_validators("one two@fish.aaaa")
            instance.run_validators("a*&@exampke.com")
            instance.run_validators("trailingwhite@example.com ")
        self.assertIsNone(instance.run_validators("mouse@example.com"))
        self.assertIsNone(instance.run_validators("mo-2use@ex3ample.com"))
        self.assertIsNone(instance.run_validators("aksdhf@sdkjf-df.cm"))

        self.assertEqual(instance.field_to_activity("test@example.com"), "test")

    def test_privacy_field_defaults(self, _):
        """post privacy field's many default values"""
        instance = fields.PrivacyField()
        self.assertEqual(instance.max_length, 255)
        self.assertEqual(
            [c[0] for c in instance.choices],
            ["public", "unlisted", "followers", "direct"],
        )
        self.assertEqual(instance.default, "public")
        self.assertEqual(
            instance.public, "https://www.w3.org/ns/activitystreams#Public"
        )

    def test_privacy_field_set_field_from_activity(self, _):
        """translate between to/cc fields and privacy"""

        @dataclass(init=False)
        class TestActivity(ActivityObject):
            """real simple mock"""

            to: List[str]
            cc: List[str]
            id: str = "http://hi.com"
            type: str = "Test"

        class TestPrivacyModel(ActivitypubMixin, BookWyrmModel):
            """real simple mock model because BookWyrmModel is abstract"""

            privacy_field = fields.PrivacyField()
            mention_users = fields.TagField(User)
            user = fields.ForeignKey(User, on_delete=models.CASCADE)

        public = "https://www.w3.org/ns/activitystreams#Public"
        data = TestActivity(
            to=[public],
            cc=["bleh"],
        )
        model_instance = TestPrivacyModel(privacy_field="direct")
        self.assertEqual(model_instance.privacy_field, "direct")

        instance = fields.PrivacyField()
        instance.name = "privacy_field"
        instance.set_field_from_activity(model_instance, data)
        self.assertEqual(model_instance.privacy_field, "public")

        data.to = ["bleh"]
        data.cc = []
        instance.set_field_from_activity(model_instance, data)
        self.assertEqual(model_instance.privacy_field, "direct")

        data.to = ["bleh"]
        data.cc = [public, "waah"]
        instance.set_field_from_activity(model_instance, data)
        self.assertEqual(model_instance.privacy_field, "unlisted")

    @patch("bookwyrm.models.activitypub_mixin.ObjectMixin.broadcast")
    @patch("bookwyrm.activitystreams.ActivityStream.add_status")
    def test_privacy_field_set_activity_from_field(self, *_):
        """translate between to/cc fields and privacy"""
        user = User.objects.create_user(
            "rat", "rat@rat.rat", "ratword", local=True, localname="rat"
        )
        public = "https://www.w3.org/ns/activitystreams#Public"
        followers = "%s/followers" % user.remote_id

        instance = fields.PrivacyField()
        instance.name = "privacy_field"

        model_instance = Status.objects.create(user=user, content="hi")
        activity = {}
        instance.set_activity_from_field(activity, model_instance)
        self.assertEqual(activity["to"], [public])
        self.assertEqual(activity["cc"], [followers])

        model_instance = Status.objects.create(
            user=user, content="hi", privacy="unlisted"
        )
        activity = {}
        instance.set_activity_from_field(activity, model_instance)
        self.assertEqual(activity["to"], [followers])
        self.assertEqual(activity["cc"], [public])

        model_instance = Status.objects.create(
            user=user, content="hi", privacy="followers"
        )
        activity = {}
        instance.set_activity_from_field(activity, model_instance)
        self.assertEqual(activity["to"], [followers])
        self.assertEqual(activity["cc"], [])

        model_instance = Status.objects.create(
            user=user,
            content="hi",
            privacy="direct",
        )
        model_instance.mention_users.set([user])
        activity = {}
        instance.set_activity_from_field(activity, model_instance)
        self.assertEqual(activity["to"], [user.remote_id])
        self.assertEqual(activity["cc"], [])

    def test_foreign_key(self, _):
        """should be able to format a related model"""
        instance = fields.ForeignKey("User", on_delete=models.CASCADE)
        Serializable = namedtuple("Serializable", ("to_activity", "remote_id"))
        item = Serializable(lambda: {"a": "b"}, "https://e.b/c")
        # returns the remote_id field of the related object
        self.assertEqual(instance.field_to_activity(item), "https://e.b/c")

    @responses.activate
    def test_foreign_key_from_activity_str(self, _):
        """create a new object from a foreign key"""
        instance = fields.ForeignKey(User, on_delete=models.CASCADE)
        datafile = pathlib.Path(__file__).parent.joinpath("../data/ap_user.json")
        userdata = json.loads(datafile.read_bytes())
        # don't try to load the user icon
        del userdata["icon"]

        # it shouldn't match with this unrelated user:
        unrelated_user = User.objects.create_user(
            "rat", "rat@rat.rat", "ratword", local=True, localname="rat"
        )

        # test receiving an unknown remote id and loading data
        responses.add(
            responses.GET,
            "https://example.com/user/mouse",
            json=userdata,
            status=200,
        )
        with patch("bookwyrm.models.user.set_remote_server.delay"):
            value = instance.field_from_activity("https://example.com/user/mouse")
        self.assertIsInstance(value, User)
        self.assertNotEqual(value, unrelated_user)
        self.assertEqual(value.remote_id, "https://example.com/user/mouse")
        self.assertEqual(value.name, "MOUSE?? MOUSE!!")

    def test_foreign_key_from_activity_dict(self, *_):
        """test recieving activity json"""
        instance = fields.ForeignKey(User, on_delete=models.CASCADE)
        datafile = pathlib.Path(__file__).parent.joinpath("../data/ap_user.json")
        userdata = json.loads(datafile.read_bytes())
        # don't try to load the user icon
        del userdata["icon"]

        # it shouldn't match with this unrelated user:
        unrelated_user = User.objects.create_user(
            "rat", "rat@rat.rat", "ratword", local=True, localname="rat"
        )
        with patch("bookwyrm.models.user.set_remote_server.delay"):
            value = instance.field_from_activity(activitypub.Person(**userdata))
        self.assertIsInstance(value, User)
        self.assertNotEqual(value, unrelated_user)
        self.assertEqual(value.remote_id, "https://example.com/user/mouse")
        self.assertEqual(value.name, "MOUSE?? MOUSE!!")
        # et cetera but we're not testing serializing user json

    def test_foreign_key_from_activity_dict_existing(self, _):
        """test receiving a dict of an existing object in the db"""
        instance = fields.ForeignKey(User, on_delete=models.CASCADE)
        datafile = pathlib.Path(__file__).parent.joinpath("../data/ap_user.json")
        userdata = json.loads(datafile.read_bytes())
        user = User.objects.create_user(
            "mouse", "mouse@mouse.mouse", "mouseword", local=True, localname="mouse"
        )
        user.remote_id = "https://example.com/user/mouse"
        user.save(broadcast=False, update_fields=["remote_id"])

        User.objects.create_user(
            "rat", "rat@rat.rat", "ratword", local=True, localname="rat"
        )

        with patch("bookwyrm.models.activitypub_mixin.ObjectMixin.broadcast"):
            value = instance.field_from_activity(activitypub.Person(**userdata))
        self.assertEqual(value, user)

    def test_foreign_key_from_activity_str_existing(self, _):
        """test receiving a remote id of an existing object in the db"""
        instance = fields.ForeignKey(User, on_delete=models.CASCADE)
        user = User.objects.create_user(
            "mouse", "mouse@mouse.mouse", "mouseword", local=True, localname="mouse"
        )
        User.objects.create_user(
            "rat", "rat@rat.rat", "ratword", local=True, localname="rat"
        )

        value = instance.field_from_activity(user.remote_id)
        self.assertEqual(value, user)

    def test_one_to_one_field(self, _):
        """a gussied up foreign key"""
        instance = fields.OneToOneField("User", on_delete=models.CASCADE)
        Serializable = namedtuple("Serializable", ("to_activity", "remote_id"))
        item = Serializable(lambda: {"a": "b"}, "https://e.b/c")
        self.assertEqual(instance.field_to_activity(item), {"a": "b"})

    def test_many_to_many_field(self, _):
        """lists!"""
        instance = fields.ManyToManyField("User")

        Serializable = namedtuple("Serializable", ("to_activity", "remote_id"))
        Queryset = namedtuple("Queryset", ("all", "instance"))
        item = Serializable(lambda: {"a": "b"}, "https://e.b/c")
        another_item = Serializable(lambda: {}, "example.com")

        items = Queryset(lambda: [item], another_item)

        self.assertEqual(instance.field_to_activity(items), ["https://e.b/c"])

        instance = fields.ManyToManyField("User", link_only=True)
        instance.name = "snake_case"
        self.assertEqual(instance.field_to_activity(items), "example.com/snake_case")

    @responses.activate
    def test_many_to_many_field_from_activity(self, _):
        """resolve related fields for a list, takes a list of remote ids"""
        instance = fields.ManyToManyField(User)
        datafile = pathlib.Path(__file__).parent.joinpath("../data/ap_user.json")
        userdata = json.loads(datafile.read_bytes())
        # don't try to load the user icon
        del userdata["icon"]

        # test receiving an unknown remote id and loading data
        responses.add(
            responses.GET, "https://example.com/user/mouse", json=userdata, status=200
        )
        with patch("bookwyrm.models.user.set_remote_server.delay"):
            value = instance.field_from_activity(
                ["https://example.com/user/mouse", "bleh"]
            )
        self.assertIsInstance(value, list)
        self.assertEqual(len(value), 1)
        self.assertIsInstance(value[0], User)

    def test_tag_field(self, _):
        """a special type of many to many field"""
        instance = fields.TagField("User")

        Serializable = namedtuple(
            "Serializable", ("to_activity", "remote_id", "name_field", "name")
        )
        Queryset = namedtuple("Queryset", ("all", "instance"))
        item = Serializable(lambda: {"a": "b"}, "https://e.b/c", "name", "Name")
        another_item = Serializable(lambda: {}, "example.com", "", "")
        items = Queryset(lambda: [item], another_item)

        result = instance.field_to_activity(items)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].href, "https://e.b/c")
        self.assertEqual(result[0].name, "Name")
        self.assertEqual(result[0].type, "Serializable")

    def test_tag_field_from_activity(self, _):
        """loadin' a list of items from Links"""
        # TODO

    @responses.activate
    @patch("bookwyrm.models.activitypub_mixin.ObjectMixin.broadcast")
    @patch("bookwyrm.suggested_users.remove_user_task.delay")
    def test_image_field(self, *_):
        """storing images"""
        user = User.objects.create_user(
            "mouse", "mouse@mouse.mouse", "mouseword", local=True, localname="mouse"
        )
        image_file = pathlib.Path(__file__).parent.joinpath(
            "../../static/images/default_avi.jpg"
        )
        image = Image.open(image_file)
        output = BytesIO()
        image.save(output, format=image.format)
        user.avatar.save("test.jpg", ContentFile(output.getvalue()))

        output = fields.image_serializer(user.avatar, alt="alt text")
        self.assertIsNotNone(
            re.match(
                r".*\.jpg",
                output.url,
            )
        )
        self.assertEqual(output.name, "alt text")
        self.assertEqual(output.type, "Document")

        instance = fields.ImageField()

        output = fields.image_serializer(user.avatar, alt=None)
        self.assertEqual(instance.field_to_activity(user.avatar), output)

        responses.add(
            responses.GET,
            "http://www.example.com/image.jpg",
            body=user.avatar.file.read(),
            status=200,
        )
        loaded_image = instance.field_from_activity("http://www.example.com/image.jpg")
        self.assertIsInstance(loaded_image, list)
        self.assertIsInstance(loaded_image[1], ContentFile)

    def test_image_serialize(self, _):
        """make sure we're creating sensible image paths"""
        ValueMock = namedtuple("ValueMock", ("url"))
        value_mock = ValueMock("/images/fish.jpg")
        result = fields.image_serializer(value_mock, "hello")
        self.assertEqual(result.type, "Document")
        self.assertEqual(result.url, "https://your.domain.here/images/fish.jpg")
        self.assertEqual(result.name, "hello")

    def test_datetime_field(self, _):
        """this one is pretty simple, it just has to use isoformat"""
        instance = fields.DateTimeField()
        now = timezone.now()
        self.assertEqual(instance.field_to_activity(now), now.isoformat())
        self.assertEqual(instance.field_from_activity(now.isoformat()), now)
        self.assertEqual(instance.field_from_activity("bip"), None)

    def test_array_field(self, _):
        """idk why it makes them strings but probably for a good reason"""
        instance = fields.ArrayField(fields.IntegerField)
        self.assertEqual(instance.field_to_activity([0, 1]), ["0", "1"])

    def test_html_field(self, _):
        """sanitizes html, the sanitizer has its own tests"""
        instance = fields.HtmlField()
        self.assertEqual(
            instance.field_from_activity("<marquee><p>hi</p></marquee>"), "<p>hi</p>"
        )
