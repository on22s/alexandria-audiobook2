"""The textured progress component's CSS (#2042).

Rules: every colour comes from the active theme's tokens (so a live theme switch
recolours the bars with no re-render), the four textures are CSS gradients and
SVG data-URI masks only (no images, no canvas, no library), the track is the
theme ink dimmed while the fill is solid ink, and nothing animates under
prefers-reduced-motion.
"""
import re
from pathlib import Path
import unittest

INDEX = Path(__file__).resolve().parent.parent / 'static/index.html'
BLOCK_START = '/* ── Textured progress bars (#2042)'
BLOCK_END = '@media (prefers-reduced-motion: reduce) {\n            .tp-fill { transition: none; }'


def component_block():
    text = INDEX.read_text(encoding='utf-8')
    start = text.index(BLOCK_START)
    end = text.index('html:is([data-theme="bread"], [data-theme="leek"])', start)
    return text[start:end]


def dot_radii(declarations):
    """Every dot radius in a `background-image` list, in layer order."""
    return [float(radius) for radius in
            re.findall(r'radial-gradient\(circle, var\(--tp-ink\) ([\d.]+)px', declarations)]


def positions(declarations):
    """The `background-position` list as (x, y) pixel pairs."""
    body = re.search(r'background-position:\s*([^;]+);', declarations).group(1)
    return [tuple(float(value[:-2] if value.endswith('px') else value) for value in part.split())
            for part in body.split(',')]


def rule(block, selector):
    """The declarations of the first rule whose selector list is exactly `selector`.

    Comments are dropped from the prelude, so a documented rule is still found
    and a comment can never be mistaken for a selector.
    """
    for match in re.finditer(r'([^{}]*)\{([^{}]*)\}', block):
        prelude = match.group(1)
        prelude = prelude[prelude.rfind('}') + 1:]
        prelude = re.sub(r'/\*.*?\*/', '', prelude, flags=re.S).strip()
        if prelude == selector:
            return match.group(2)
    raise AssertionError('no rule for %r' % selector)


class TexturedProgressCssTests(unittest.TestCase):
    def test_the_block_documents_one_texture_per_meaning(self):
        block = component_block()
        for texture in ('ticks', 'dither', 'halftone', 'hatch'):
            with self.subTest(texture=texture):
                self.assertIn(texture, block.split('*/', 1)[0])
                self.assertIn('.tp-%s {' % texture, block)

    def test_every_theme_defines_the_bar_ink_from_its_own_tokens(self):
        text = INDEX.read_text(encoding='utf-8')
        block = component_block()
        # The ink is the theme accent, and each of the six themes really does
        # carry the colour the design names, so this cannot silently drift.
        accents = {
            'light': '#0d6efd', 'night': '#6ea8fe', 'super-night': '#8ab4f8',
            'cyberpunk': '#00f0ff', 'bread': '#aa294d', 'leek': '#39c5bb',
        }
        for theme, accent in accents.items():
            with self.subTest(theme=theme):
                selector = (r'\n\s*html\[data-theme="%s"\]\s*\{([^{}]*)\}'
                            % theme) if theme != 'light' else r'\n\s*:root\s*\{([^{}]*)\}'
                bodies = re.findall(selector, text)
                self.assertTrue(bodies, theme)
                self.assertTrue(any(re.search(r'--accent:\s*%s;' % accent, body)
                                    for body in bodies),
                                'no %s --accent in %s' % (accent, bodies))

        # The bar's ink resolves through --accent for five themes and through
        # cyberpunk's neon triple; nothing else may name a colour.
        self.assertIn('--tp-ink: var(--accent);', block)
        self.assertIn('--tp-ink: var(--cyber-cyan);', block)
        for texture, neon in (('dither', 'pink'), ('halftone', 'yellow'), ('hatch', 'cyan')):
            with self.subTest(texture=texture):
                self.assertIn('html[data-theme="cyberpunk"] .tp-%s { --tp-ink: var(--cyber-%s); }'
                              % (texture, neon), block)

    def test_the_component_names_no_literal_colour(self):
        # A hardcoded colour would survive a live theme switch as the wrong ink,
        # which is the one thing the component must not do. #000 is allowed: it
        # is a mask (alpha), not a paint.
        in_comment = False
        for line in component_block().splitlines():
            stripped = line.strip()
            if in_comment:
                in_comment = '*/' not in stripped
                continue
            if stripped.startswith('/*'):
                in_comment = '*/' not in stripped
                continue
            for hexcode in re.findall(r'#[0-9a-fA-F]{3,8}\b', line):
                with self.subTest(line=line.strip(), colour=hexcode):
                    if hexcode == '#000':
                        continue
                    self.assertIn('--tp-ink-text:', line,
                                  'only the text colour on a solid fill may be literal')

    def test_the_track_is_dim_ink_and_the_fill_is_solid_ink(self):
        block = component_block()
        track = rule(block, '.tp::before')
        self.assertIn('background-color: var(--tp-ink);', track)
        self.assertIn('opacity: var(--tp-dim);', track)
        self.assertIn('mask: var(--tp-tex);', track)
        fill = rule(block, '.tp-fill')
        self.assertIn('background-color: var(--tp-ink);', fill)
        # The same ink twice means the dimmed track composites onto the solid
        # fill as the fill's own colour, so no card-coloured cover band exists.
        self.assertNotIn('--bg-card', block)
        self.assertNotIn('--bg-page', block)

    def test_the_bar_is_28px_tall_with_square_corners(self):
        bar = rule(component_block(), '.tp')
        self.assertIn('height: 28px;', bar)
        self.assertIn('border-radius: 0;', bar)
        self.assertIn('height: 20px;', rule(component_block(), '.tp-sm'))

    def test_each_texture_is_a_gradient_or_an_svg_mask(self):
        block = component_block()
        ticks = rule(block, '.tp-ticks')
        self.assertIn('repeating-linear-gradient(90deg, #000 0 2px, transparent 2px 8px)',
                      ticks)
        # 4px lead tick, 3px after the fill edge.
        lead = rule(block, '.tp-ticks .tp-lead')
        self.assertIn('width: 4px;', lead)
        self.assertIn('margin-left: 3px;', lead)

        dither = rule(block, '.tp-dither')
        self.assertIn('data:image/svg+xml', dither)
        self.assertIn('width=\'24\' height=\'28\'', dither)
        self.assertEqual(dither.count('width=\'2\' height=\'2\''), 15, 'irregular 2px pixels')
        # A 1px-wide column of 2px pixels is the fray at the fill edge.
        self.assertIn('rect x=\'0\' y=\'0\' width=\'2\' height=\'28\'',
                      rule(block, '.tp-dither .tp-lead'))

        # Halftone dots are CIRCLES (a conic-gradient would make them squares)
        # on the same offset lattice, and the fill edge is a 9px linear fade.
        halftone = rule(block, '.tp-halftone')
        self.assertEqual(2, halftone.count('radial-gradient(circle, #000 1.15px'))
        self.assertIn('/ 6px 6px', halftone)
        self.assertIn('3px 3px / 6px 6px', halftone, 'the lattice is offset, not square')
        self.assertNotIn('conic-gradient', halftone)
        lead = rule(block, '.tp-halftone .tp-lead')
        self.assertIn('width: 9px;', lead)
        self.assertIn('linear-gradient(90deg, var(--tp-ink), transparent)', lead)
        self.assertIn('radial-gradient(circle', rule(block, '.tp-halftone .tp-lead2'))
        self.assertNotIn('conic-gradient', rule(block, '.tp-halftone .tp-lead2'))

        hatch = rule(block, '.tp-hatch')
        self.assertIn('repeating-linear-gradient(-55deg, #000 0 1.6px, transparent 1.6px 7px)',
                      hatch)
        self.assertIn('width: 8px;', rule(block, '.tp-hatch .tp-lead'))

    def test_the_halftone_edge_is_round_dots_shrinking_on_the_offset_lattice(self):
        """Rebuild the fill-edge dot lattice from the declarations and check it.

        A radial-gradient dot sits at the centre of its 6px tile, so
        position + tile centre gives every dot's centre. The stripes this
        replaced put a band's two layers at the SAME x (two dots 3px apart
        vertically, i.e. a column) and had one radius per column -- both
        assertions below reject that: the x positions must step 3px, and one
        radius may cover only one lattice period (two dots).
        """
        block = component_block()
        lead2 = rule(block, '.tp-halftone .tp-lead2')
        self.assertIn('background-size: 6px 6px;', lead2, 'one lattice period')
        self.assertIn('background-repeat: repeat-y;', lead2)
        period = 6.0
        radii = dot_radii(lead2)
        placed = positions(lead2)
        self.assertEqual(len(radii), len(placed), 'one position per dot layer')
        self.assertEqual(12, len(radii))
        dots = sorted((round(x + period / 2, 2), round((y + period / 2) % period, 2), radius)
                      for (x, y), radius in zip(placed, radii))

        # One diagonal lattice: dots every 3px in x, alternating 3px in y.
        self.assertEqual([3.0 + 3 * step for step in range(12)], [x for x, _, _ in dots],
                         'dot columns must step 3px, never repeat the same x')
        self.assertEqual([3.0 if index % 2 == 0 else 0.0 for index in range(12)],
                         [y for _, y, _ in dots], 'the second dot of a band is offset 3px')

        # Six radii, shrinking left to right, one lattice period (two dots) each.
        self.assertEqual([1.85, 1.85, 1.71, 1.71, 1.57, 1.57,
                          1.43, 1.43, 1.29, 1.29, 1.15, 1.15],
                         [radius for _, _, radius in dots])
        # Dots here are 4.24px apart, so a step above ~1.9px touches its
        # neighbour and the merged pair renders as a solid slab -- the very
        # columns these steps exist to avoid.
        self.assertLess(max(radii), 4.24 / 2, 'steps must stay clear of each other')
        ordered = sorted(set(radii), reverse=True)
        self.assertEqual(6, len(ordered), 'five shrinking steps plus the track dot')
        for radius in ordered:
            with self.subTest(radius=radius):
                self.assertEqual(2, radii.count(radius), 'one lattice period per radius')

        # The ramp's first dot is large and its last is the track's own dot, so
        # the two lattices meet in phase: same dots modulo one period.
        track = rule(block, '.tp-halftone')
        track_dots = set()
        for x, y in re.findall(r'\)\s*(-?[\d.]+)(?:px)?\s+(-?[\d.]+)(?:px)?\s*/\s*6px 6px', track):
            track_dots.add((round((float(x) + 3) % period, 2),
                            round((float(y) + 3) % period, 2)))
        self.assertEqual({(3.0, 3.0), (0.0, 0.0)}, track_dots)
        self.assertEqual(track_dots, {(x % period, y % period) for x, y, _ in dots},
                         "the ramp is the track's own lattice")
        self.assertEqual(1.15, min(radii), "the ramp ends at the track's dot")

    def test_no_texture_needs_an_image_file_or_a_script(self):
        block = component_block()
        self.assertIsNone(re.search(r'url\((?!["\']?data:image/svg\+xml)', block))
        self.assertNotIn('<canvas', block)
        self.assertNotIn('background-image: url("/static', block)

    def test_indeterminate_and_reduced_motion(self):
        block = component_block()
        self.assertIn('.tp-indeterminate .tp-fill,', block)
        self.assertIn('.tp-indeterminate::before { animation: tp-track-slide 3.2s linear infinite; }',
                      block)
        self.assertIn('@keyframes tp-track-slide', block)
        reduced = block[block.index('@media (prefers-reduced-motion: reduce)'):]
        self.assertIn('.tp-fill { transition: none; }', reduced)
        self.assertIn('.tp-indeterminate::before { animation: none; }', reduced)

    def test_the_bootstrap_bar_styles_are_gone(self):
        text = INDEX.read_text(encoding='utf-8')
        self.assertNotIn('#full-progress-bar {', text)
        self.assertIsNone(re.search(r'\.progress(-\w+)?\s*[{,]', text),
                          'no Bootstrap progress selector may survive')
