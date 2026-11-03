"""Contrast checks for the native and report palettes, including control states."""
from pathlib import Path
import re

import pytest

from deckard.theme import PALETTES, stylesheet


def luminance(color):
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    channels = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
    return sum(c * weight for c, weight in zip(channels, (.2126, .7152, .0722)))


def contrast(first, second):
    low, high = sorted((luminance(first), luminance(second)))
    return (high + .05) / (low + .05)


def report_palettes():
    css = (Path(__file__).parents[1] / 'deckard/report/web/style.css').read_text()
    return [dict((name.replace('-', '_'), value) for name, value in
                 re.findall(r'--([\w-]+): (#[0-9a-f]{6});', block))
            for block in re.findall(r':root(?:\[data-theme="dark"\])? \{([^}]+)\}', css)]


@pytest.mark.parametrize('mode', ['light', 'dark'])
def test_text_and_controls_meet_wcag_aa(mode):
    colors = PALETTES[mode]
    text_pairs = [(ink, surface) for ink in ('ink', 'muted', 'disabled')
                  for surface in ('face', 'sidebar', 'panel', 'field', 'hover', 'pressed', 'row', 'row_alt')]
    text_pairs += [('on_accent', 'accent'), ('on_accent', 'accent_hover'),
                   ('placeholder', 'field'), ('log_ink', 'log'), ('metric', 'log')]
    for ink, background in text_pairs:
        assert contrast(colors[ink], colors[background]) >= 4.5, (mode, ink, background)
    assert contrast('#ffffff', colors['section']) >= 4.5
    for surface in ('face', 'panel', 'field', 'row', 'row_alt'):
        for token in ('line', 'focus'):
            assert contrast(colors[token], colors[surface]) >= 3, (mode, token, surface)
    assert '@' not in stylesheet(mode)
    web = report_palettes()[0 if mode == 'light' else 1]
    # Keep the two rendering systems consistent.
    for token, value in colors.items():
        assert web[token] == value, (mode, token)
    for ink, background in [('ink', 'coverage'), ('muted', 'table'),
                            ('pill_ink', 'pill'), ('keeper_ink', 'keeper')]:
        assert contrast(web[ink], web[background]) >= 4.5, (mode, ink, background)


def test_fixed_matrix_axis_and_loading_text_meet_aa():
    for ink, surface in [('#e0eaf8', '#263b56'), ('#e0eaf8', '#203149'),
                         ('#afc5e2', '#0b111b'), ('#c4ddf5', '#0b111b')]:
        assert contrast(ink, surface) >= 4.5
