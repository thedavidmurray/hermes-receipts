# Receipt themes

bar colour derives from paper; never hardcode

`green_bar` requests alternating bands. Optional `bar` is an `#rrggbb` colour;
otherwise it is paper blended 6% toward the main ink in sRGB channels. Bands are
optional furniture, not verdict semantics: the renderer skips them on dark paper
(relative luminance ≤ 0.5), or when any rendered text ink would fall below 4.5:1
against the band. It does not recolour verdicts to accommodate a band.

Monochrome contradicted claims retain inverse video. Category count lines use
inverse video only for a count above zero, including report counts and ASCII
wrapping. A zero still prints the same verdict word, glyph and ink, without a
failure rectangle.

## Verdict colour roles

Non-monochrome themes may tune shades, but not their semantic families. These
roles are enforced in code: supported is green (HSV hue 90–165 degrees),
contradicted is red (345–360 or 0–15), unverified is amber (30–65), and
not-evaluable is neutral (saturation at most 20%). Chromatic roles require at
least 25% saturation. Every verdict still needs its own ink and 4.5:1 paper
contrast; swapping two otherwise legible inks is rejected. Declared monochrome
themes use exactly the main ink for every verdict and retain words/glyphs.

Keys beginning with hide, soften, suppress, mute, verdict, glyph, relabel,
rename or remap are rejected, including unlisted family members. Only the exact
`glyph_set` selector is exempt, and only for the code-owned ascii/unicode tables.

`tests/test_v2_review_repairs.py` covers all four required verdicts on both row
parities, all pairwise semantic-role swaps in both shipped colour themes,
custom valid shades, forbidden-key families and the explicit glyph selector.

## Regression provenance

The reported black-paper/mint-band PNG came from the verifier's synthetic
`invisible` theme (`t_c4b408de` attachment `probe.py`, lines 13–19): a copy of
`thermal-classic` with black paper, white monochrome verdict inks and
`green_bar: true`. It was not a desktop default, shipped theme, or user config.
The original PNG's SHA256 is
`8a1783980260e8d192301b51042ad4c4e07f90347f28fda5877f8a18e2dde4f2`.
The renderer now safely suppresses bands even for that exact adversarial theme;
an ordinary dark theme should leave `green_bar` false. Historical verifier
attachments are preserved unchanged as evidence. The regression test uses the
original `green_bar: true` request to ensure this cannot recur.
