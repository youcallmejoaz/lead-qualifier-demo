from src.evidence import QuoteStatus, build_corpus, normalise, verify_assessment, verify_quote
from src.models import EvidenceQuote, Pillar


def test_invented_quote_is_dropped(load_account):
    corpus = build_corpus(load_account("sample_qualified"))
    check = verify_quote(EvidenceQuote(quote="I personally coach all 140 clients every week", source_id="p03"), corpus)
    assert check.status is QuoteStatus.dropped
    assert not check.found and not check.counts


def test_exact_quote_is_verified_with_date(load_account):
    corpus = build_corpus(load_account("sample_qualified"))
    check = verify_quote(EvidenceQuote(quote="Round 4 SOLD OUT in 48 hours", source_id="p02"), corpus)
    assert check.status is QuoteStatus.verified
    assert check.date == "2026-08-07"


def test_curly_quotes_case_and_whitespace_still_verify(load_account):
    corpus = build_corpus(load_account("sample_qualified"))
    check = verify_quote(EvidenceQuote(quote="Sent!  Sorry it took a day, I’m DOING these by hand", source_id="p05"),
                         corpus)
    assert check.status is QuoteStatus.verified


def test_wrong_post_id_is_corrected(load_account):
    corpus = build_corpus(load_account("sample_qualified"))
    check = verify_quote(EvidenceQuote(quote="He's back with me in round 4 to build his team.", source_id="p03"), corpus)
    assert check.status is QuoteStatus.corrected
    assert check.source_id == "p04"


def test_quote_cannot_straddle_two_comments(load_account):
    corpus = build_corpus(load_account("sample_qualified"))
    # end of one comment + start of the owner's reply
    check = verify_quote(EvidenceQuote(quote="3 to 9 patients a day Yes! Priya", source_id="p01"), corpus)
    assert check.status is QuoteStatus.dropped


def test_ellipsis_fragments_must_appear_in_order(load_account):
    corpus = build_corpus(load_account("sample_qualified"))
    ok = verify_quote(EvidenceQuote(quote="Delivering round 4 ... I write every client's weekly review myself",
                                    source_id="p03"), corpus)
    bad = verify_quote(EvidenceQuote(quote="I write every client's weekly review myself ... Delivering round 4",
                                     source_id="p03"), corpus)
    assert ok.status is QuoteStatus.verified
    assert bad.status is QuoteStatus.dropped


def test_metrics_quote_handles_thousand_separators(load_account):
    corpus = build_corpus(load_account("sample_qualified"))
    assert verify_quote(EvidenceQuote(quote="followers: 18,400", source_id="metrics"), corpus).found


def test_bio_quote_is_found_but_never_counts(load_account, load_assessment, rubric):
    va = verify_assessment(load_assessment("sample_weak_offer"), load_account("sample_weak_offer"), rubric)
    offer = va.pillar(Pillar.offer_works)
    assert offer.verified == 1 and offer.claimed == 1  # quote is real...
    assert not offer.has_evidence  # ...but it is from the bio, so it is not evidence
    assert not offer.triggers[0].matched


def test_confidence_drops_with_invented_quotes(load_account, load_assessment, rubric):
    va = verify_assessment(load_assessment("sample_qualified"), load_account("sample_qualified"), rubric)
    bottleneck = va.pillar(Pillar.founder_bottleneck)
    assert (bottleneck.verified, bottleneck.claimed) == (8, 9)
    assert round(bottleneck.confidence, 3) == 0.889
    assert va.pillar(Pillar.offer_works).confidence == 1.0


def test_normalise():
    assert normalise("  It’s “GREAT”\n\n 12,500 ") == "it's \"great\" 12500"
