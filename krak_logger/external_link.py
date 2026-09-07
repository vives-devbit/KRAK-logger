"""Couples our Start Recording button to NexygenPlus 4.1's Start button.

Why this posts a click to a control instead of sending F5
---------------------------------------------------------
The obvious approach - focus the NexygenPlus window and inject F5 - does not
work here. NexygenPlus 4.1 is an MFC app with a RibbonBar and no classic menu
(GetMenu returns 0), and part of its UI is hosted in a *second process*: the
console panel showing live force/extension readings has a different PID from
the frame window. A synthetic F5 lands on whichever window holds keyboard
focus, and in that arrangement it never reached a message loop that translates
it into the Start command. It also meant stealing the foreground on every
recording, which is disruptive when working split-screen.

Instead we find the real Win32 'Start' button in the window tree and post the
same WM_COMMAND/BN_CLICKED notification the button itself would send to its
parent dialog. That needs no focus and no foreground switch, so the KRAK GUI
keeps focus, and it does not care how the app routes accelerators.

Observed layout (NexygenPlus 4.1, test document open):

    NexygenPlus 4.1 - Licensed - [<document>]   Afx:...:000303E3   frame
      MDIClient
        <document>                              Afx:...:000A04E3
          ...
            #32770                              dialog
              Button 'Edit'    id 3771
              Button 'Start'   id 3773   <- this one
              Button 'Results' id 3920

Control IDs are not relied on (they may differ per document type); the button
is located by class + caption, and its ID is only logged.
"""
from __future__ import annotations

import ctypes

import win32con
import win32gui

user32 = ctypes.WinDLL("user32", use_last_error=True)

NEXYGENPLUS_TITLE_PREFIX = "NexygenPlus"
START_BUTTON_CAPTION = "start"

BN_CLICKED = 0


def _find_main_window(title_prefix=NEXYGENPLUS_TITLE_PREFIX):
    """Top-level NexygenPlus frame window, or None."""
    found = []

    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd).startswith(title_prefix):
            found.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else None


def _find_start_button(top_hwnd):
    """The 'Start' Button control anywhere under top_hwnd, or None.

    EnumChildWindows walks the whole descendant tree, not just direct
    children, which is what reaches the button inside the MDI document's
    dialog.
    """
    found = []

    def _cb(hwnd, _):
        try:
            if (win32gui.GetClassName(hwnd) == "Button"
                    and win32gui.GetWindowText(hwnd).strip().lower() == START_BUTTON_CAPTION):
                found.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumChildWindows(top_hwnd, _cb, None)
    return found[0] if found else None


def _post_click(button_hwnd):
    """Post the BN_CLICKED notification the button would send when pressed.

    Sent to the parent dialog rather than BM_CLICK to the button, because
    BM_CLICK is documented to fail when the containing dialog is not active -
    and not activating anything is the whole point here. PostMessage, not
    SendMessage: if the click puts up a modal dialog, a synchronous send would
    block our Tk thread until someone dismissed it.
    """
    parent = win32gui.GetParent(button_hwnd)
    ctrl_id = user32.GetDlgCtrlID(button_hwnd)
    wparam = (ctrl_id & 0xFFFF) | ((BN_CLICKED & 0xFFFF) << 16)
    win32gui.PostMessage(parent, win32con.WM_COMMAND, wparam, button_hwnd)
    return ctrl_id


def check_nexygenplus_ready():
    """Can NexygenPlus's Start button be pressed right now? Presses nothing.

    Returns (ready, detail). Split from the press itself so a caller can gate a
    whole operation on NexygenPlus being startable *before* taking any action
    it would then have to unwind - clicking Start is not reversible, so it has
    to be the last thing that happens, not the thing that discovers a problem.
    """
    top = _find_main_window()
    if top is None:
        return False, "no NexygenPlus window found - is NexygenPlus 4.1 running?"

    title = win32gui.GetWindowText(top)
    button = _find_start_button(top)
    if button is None:
        return False, f"no 'Start' button found in {title!r} - is a test document open?"

    if not win32gui.IsWindowEnabled(button):
        return False, ("the 'Start' button is greyed out - a test may already be "
                       "running, or the test is not ready to run")

    return True, f"'Start' ready (hwnd {button}) in {title!r}"


def trigger_nexygenplus_start():
    """Press NexygenPlus 4.1's Start button without taking focus.

    Returns (ok, detail). Re-checks the button rather than trusting an earlier
    check_nexygenplus_ready(), since the window can change in between.
    """
    top = _find_main_window()
    if top is None:
        return False, "no NexygenPlus window found"

    title = win32gui.GetWindowText(top)
    button = _find_start_button(top)
    if button is None:
        return False, f"no 'Start' button in {title!r} (is a test document open?)"

    if not win32gui.IsWindowEnabled(button):
        return False, "the 'Start' button is greyed out (a test may already be running)"

    ctrl_id = _post_click(button)
    return True, f"clicked 'Start' (hwnd {button}, id {ctrl_id}) in {title!r}"
