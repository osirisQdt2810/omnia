"""The page the web-clipper install opens, because Chrome will not open its own.

Chrome drops a ``chrome://`` URL passed on the command line (measured on Chrome 152, macOS and
Windows, for every spelling including ``--app=`` and ``chrome://settings/``) and opens the
new-tab page instead, while ``file://`` in the same position opens normally. So the last step
is a local page — and a page is only worth opening if the two things it must carry, the folder
and the address, come through intact on a Windows path as well as a POSIX one.
"""

from __future__ import annotations

import re

from omnia.plugins.smart_notes.integration.install_page import (
    EXTENSIONS_URL,
    render_install_page,
)


class TestWhatThePageMustCarry:
    def test_it_names_the_folder_to_load(self):
        html = render_install_page("/home/phuc/clippers/web_clipper")

        assert "/home/phuc/clippers/web_clipper" in html

    def test_it_names_the_address_the_user_has_to_paste(self):
        """Chrome blocks a LINK to chrome:// from a page as firmly as from the command line,
        so the address has to be readable text the user can copy, not an ``<a href>``.
        """
        html = render_install_page("/tmp/web_clipper")

        assert EXTENSIONS_URL in html
        assert f'href="{EXTENSIONS_URL}' not in html

    def test_it_spells_out_the_manual_step(self):
        html = render_install_page("/tmp/web_clipper")

        assert "Developer mode" in html
        assert "Load unpacked" in html

    def test_it_names_the_profile_it_opened_in(self):
        """A user with eight profiles has to see WHICH one is getting the extension."""
        html = render_install_page("/tmp/web_clipper", "moreh.com.vn")

        assert "moreh.com.vn" in html

    def test_an_unknown_profile_does_not_leave_a_dangling_sentence(self):
        html = render_install_page("/tmp/web_clipper")

        assert "<strong></strong>" not in html
        assert "profile" in html  # still says where the extension will land


class TestPathsThatWouldOtherwiseBreakIt:
    """A path reaches the page twice: as text, and inside a JavaScript string literal."""

    def test_a_windows_path_survives_the_copy_button(self):
        r"""``C:\Users\...`` inside a JS string literal is ``\U`` — a broken escape that takes
        the whole script down, so the only buttons on the page stop working on the one platform
        whose paths contain backslashes. The page therefore reads what to copy back out of the
        DOM, and the path reaches the document only as text.
        """
        path = r"C:\Users\PC\AppData\Roaming\clippers\web_clipper"

        page = render_install_page(path)

        script = page[page.index("<script>") :]
        assert path not in script, "the path must never reach a JavaScript literal"
        assert f'<code id="omnia-path">{path}</code>' in page
        assert 'button data-copy="omnia-path"' in page

    def test_every_copy_button_points_at_an_element_that_exists(self):
        page = render_install_page("/tmp/web_clipper")

        targets = re.findall(r'button data-copy="([^"]+)"', page)
        assert targets, "the page must have copy buttons"
        for target in targets:
            assert f'id="{target}"' in page

    def test_a_path_with_html_in_it_cannot_rewrite_the_page(self):
        html = render_install_page("/tmp/<script>alert(1)</script>/web_clipper")

        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_an_ampersand_in_the_path_is_escaped(self):
        html = render_install_page("/tmp/tools & toys/web_clipper")

        assert "tools &amp; toys" in html


class TestItWorksOverFile:
    def test_it_pulls_in_no_external_asset(self):
        """It is opened over ``file://``: a stylesheet or script fetched from a host would
        simply not arrive, and the page would render as unstyled text with a dead button.
        """
        html = render_install_page("/tmp/web_clipper")

        assert "http://" not in html
        assert "https://" not in html
        assert "<style>" in html  # the styling is inline, where it can survive

    def test_it_is_a_complete_document(self):
        html = render_install_page("/tmp/web_clipper")

        assert html.startswith("<!doctype html>")
        assert html.rstrip().endswith("</html>")


class TestReachingAFolderThePickerHides:
    """The path alone does not answer "how do I get there?", and that is where people stop.

    Every platform puts this clone inside a hidden directory. On macOS `~/Library` carries the
    hidden flag, so Chrome's open panel does not list it at all — and the panel's sidebar shows
    a `Library` that is the SYSTEM one, a different folder with no Anki2 in it. Someone
    following the path lands there, finds nothing, and concludes the install failed. It is the
    single reported failure of this page.
    """

    def test_macos_names_the_shortcut_and_says_why_browsing_fails(self):
        html = render_install_page("/tmp/web_clipper", platform="darwin")
        assert "&#8984;" in html  # ⌘
        assert "&#8679;" in html  # ⇧
        assert "hides your <code>Library</code>" in html

    def test_macos_warns_that_the_sidebar_library_is_a_different_one(self):
        # Landing in /Library and finding no Anki2 is the exact wrong turn to head off.
        html = render_install_page("/tmp/web_clipper", platform="darwin")
        assert "different one" in html

    def test_windows_says_paste_into_the_address_bar(self):
        html = render_install_page("/tmp/web_clipper", platform="win32")
        assert "address bar" in html
        assert "&#8984;" not in html  # no macOS keys on Windows

    def test_linux_names_ctrl_l_and_the_hidden_dot_folder(self):
        html = render_install_page("/tmp/web_clipper", platform="linux")
        assert "Ctrl" in html
        assert ".local" in html

    def test_an_unknown_platform_still_gets_a_way_in(self):
        html = render_install_page("/tmp/web_clipper", platform="freebsd13")
        assert "paste the path" in html

    def test_the_running_platform_is_used_when_none_is_given(self):
        import sys

        assert render_install_page("/tmp/web_clipper") == render_install_page(
            "/tmp/web_clipper", platform=sys.platform
        )
