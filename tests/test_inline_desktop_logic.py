"""Execute the real QTextDocument serialization handler against UTF-16 fixtures; no Qt."""
import types
import unittest
from twonote import inline
from test_desktop_controls_logic import handlers


class Fragment:
    def __init__(self, position, text, image=None): self.position_value, self.value, self.image = position, text, image
    def position(self): return self.position_value
    def length(self): return len(self.value.encode("utf-16-le")) // 2
    def text(self): return self.value
    def charFormat(self):
        return types.SimpleNamespace(isImageFormat=lambda: self.image is not None,
            toImageFormat=lambda: types.SimpleNamespace(name=lambda: self.image))


class Iterator:
    def __init__(self, fragments): self.fragments, self.index = fragments, 0
    def atEnd(self): return self.index == len(self.fragments)
    def fragment(self): return self.fragments[self.index]
    def __iadd__(self, value): self.index += value; return self


class Block:
    def __init__(self, position, fragments): self.position_value, self.fragments, self.following = position, fragments, None
    def isValid(self): return True
    def position(self): return self.position_value
    def length(self): return sum(f.length() for f in self.fragments) + 1
    def begin(self): return Iterator(self.fragments)
    def next(self): return self.following or types.SimpleNamespace(isValid=lambda: False)


class DesktopInlineTests(unittest.TestCase):
    def setUp(self):
        self.id = "a" * 64
        first = Block(0, [Fragment(0, "A😀"), Fragment(3, "\ufffc", "twonote-embed://" + self.id), Fragment(4, "B")])
        second = Block(6, []); third = Block(7, [Fragment(7, "C")]); first.following = second; second.following = third
        note_type = handlers("NoteText", {"portable_text", "saved_content"}, {"inline": inline})
        self.note = note_type(); self.note.document = lambda: types.SimpleNamespace(begin=lambda: first, characterCount=lambda: 9)
        self.note.inline_mode = True

    def test_serialization_preserves_images_blank_lines_and_emoji(self):
        self.assertEqual(self.note.saved_content(), inline.encode("A😀" + inline.token(self.id) + "B\n\nC"))

    def test_copy_image_and_copy_emoji_use_qt_utf16_positions(self):
        self.assertEqual(self.note.portable_text(3, 4), inline.token(self.id))
        self.assertEqual(self.note.portable_text(1, 3), "😀")

    def test_partial_selection_and_trailing_line_break(self):
        self.assertEqual(self.note.portable_text(3, 7), inline.token(self.id) + "B\n\n")
        self.assertEqual(self.note.portable_text(0, 0), "")


if __name__ == "__main__": unittest.main()
