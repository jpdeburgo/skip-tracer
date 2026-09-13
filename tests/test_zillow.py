from skip_tracer.zillow import zillow_search_link


def test_zillow_search_link_matches_confirmed_slug_format():
    # Ground truth from planning: this street/city/state/zip combination's
    # canonical zpid URL is confirmed to be
    # .../7924-Lakenheath-Way-Rockville-MD-20854/37267570_zpid/ — the search
    # (zpid-free) slug below should match that same hyphenated segment.
    link = zillow_search_link("7924 Lakenheath Way", "Rockville", "MD", "20854")
    assert link == "https://www.zillow.com/homes/7924-Lakenheath-Way-Rockville-MD-20854_rb/"


def test_zillow_search_link_uses_premise_city_not_county_city():
    # MD iMap's CITY and PREMCITY can disagree (confirmed: CITY=Potomac,
    # PREMCITY=Rockville for this same record) — callers must pass PREMCITY.
    link = zillow_search_link("7924 Lakenheath Way", "Potomac", "MD", "20854")
    assert "Potomac" in link
    assert "Rockville" not in link
