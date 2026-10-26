import unittest
from datetime import timedelta

from bot import display, mute_arguments, parse_duration, reason, username_arguments


class ModerationHelpersTests(unittest.TestCase):
    def test_duration_allows_space_between_number_and_unit(self):
        self.assertEqual(parse_duration("1 h"), timedelta(hours=1))
        self.assertEqual(parse_duration("15m"), timedelta(minutes=15))
        self.assertEqual(parse_duration("2d"), timedelta(days=2))

    def test_mute_duration_and_multiline_reason(self):
        duration, text = mute_arguments("1 h\nфлуд в чате")
        self.assertEqual(duration, timedelta(hours=1))
        self.assertEqual(text, "флуд в чате")

    def test_mute_reason_without_duration_defaults_to_one_hour(self):
        duration, text = mute_arguments("флуд")
        self.assertEqual(duration, timedelta(hours=1))
        self.assertEqual(text, "флуд")

    def test_invalid_mute_duration_is_rejected(self):
        self.assertEqual(mute_arguments("1 q причина"), (None, None))

    def test_html_in_user_content_is_escaped(self):
        class User:
            id = 42
            full_name = "<admin> & friends"

        self.assertIn("&lt;admin&gt; &amp; friends", display(User()))
        self.assertEqual(reason("<b>test</b>"), "&lt;b&gt;test&lt;/b&gt;")

    def test_ban_username_and_multiline_reason(self):
        self.assertEqual(
            username_arguments("@Some_User\nоскорбления"),
            ("some_user", "оскорбления"),
        )

    def test_invalid_username_target_is_not_parsed(self):
        self.assertEqual(username_arguments("@bad! причина"), (None, None))


if __name__ == "__main__":
    unittest.main()
