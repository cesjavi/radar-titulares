"""Las búsquedas estructuradas respetan palabras completas."""

from radar.text import contains_search_term, tokenize_for_search


def test_search_terms_do_not_match_substrings():
    tokens = tokenize_for_search("El estado de São Paulo y sus estadísticas")
    assert contains_search_term(tokens, "estado")
    assert contains_search_term(tokens, "sao paulo")
    assert not contains_search_term(tokens, "estad")
    assert not contains_search_term(tokens, "palo")
