-- Migration: add 'shitty_positional_match' as an allowed match_method.
--
-- A deliberately low-confidence positional fallback (sync/positional_fallback.py)
-- for records that already carry raw coordinates but matched neither by
-- identifier (name_resolved/direct_gaia_column) nor by matcher.py's tight
-- 1" positional_easy_match. Widens the search to 60" against both our own
-- tracked stars and a live Gaia DR3 cone search, picks a candidate via a
-- brightness/BSC5-bright-star/faintness-ceiling rule, and always lands in
-- needs_review -- never 'matched' -- since the underlying signal is an
-- inference (nearest/brightest plausible star), not a confirmed identifier.
-- See project design discussion, 2026-08-17.
--
-- Run inside a transaction against the live database.

BEGIN;

ALTER TABLE spectroscopy_holdings DROP CONSTRAINT spectroscopy_holdings_match_method_check;
ALTER TABLE spectroscopy_holdings ADD CONSTRAINT spectroscopy_holdings_match_method_check CHECK (match_method IN (
    'direct_gaia_column',
    'name_resolved',
    'positional_easy_match',
    'shitty_positional_match',
    'lr_matched',
    'manual'
));

COMMIT;

-- Application code (sync/positional_fallback.py, scripts/shitty_positional_match.py)
-- already updated to match this schema in the same PR that added this
-- migration -- deploy alongside it.
