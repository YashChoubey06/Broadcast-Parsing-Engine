import json
import os
import sys

import pandas as pd
import streamlit as st


sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src import phase4_review as review


st.set_page_config(page_title="Phase 4 Candidate Review", layout="wide")


ACTION_TO_STATUS = {
    "Confirm Ordered Close Then Entry": review.CONFIRMED_ORDERED_CLOSE_THEN_ENTRY,
    "Confirm Multi-Clause Review Only": review.CONFIRMED_MULTI_CLAUSE_REVIEW_ONLY,
    "Mark False Positive": review.FALSE_POSITIVE,
    "Needs More Context": review.NEEDS_MORE_CONTEXT,
    "Do Not Use for Training": review.DO_NOT_USE_FOR_TRAINING,
    "Reject Gibberish": review.REJECTED_GIBBERISH,
}


def _options(rows, field):
    values = sorted({row.get(field, "") for row in rows if row.get(field, "")})
    return ["ALL"] + values


def _json_value(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _show_json(label, value):
    st.caption(label)
    parsed = _json_value(value)
    if parsed in (None, ""):
        st.code("")
    else:
        st.json(parsed)


def _save(
    candidate,
    status,
    reviewer,
    notes,
    use_for_structure_training,
    use_for_portfolio_effect_training,
    portfolio_effect_status,
    corrected,
):
    try:
        saved = review.save_decision(
            candidate["candidate_id"],
            review_status=status,
            use_for_structure_training=use_for_structure_training,
            use_for_portfolio_effect_training=use_for_portfolio_effect_training,
            portfolio_effect_status=portfolio_effect_status,
            reviewer=reviewer,
            review_notes=notes,
            corrected_fields=corrected,
        )
        review.write_review_reports()
        st.success(f"Saved {saved['review_status']}")
        st.rerun()
    except Exception as exc:
        st.error(str(exc))


def main():
    st.title("Phase 4 Candidate Human Review")

    try:
        candidates = review.load_candidates()
        decisions = review.load_decisions()
    except Exception as exc:
        st.error(str(exc))
        return

    rows = review.attach_decisions(candidates, decisions)
    summary = review.progress_summary()

    with st.sidebar:
        st.header("Filters")
        reviewer = st.text_input("Reviewer")
        review_status = st.selectbox("Review status", ["ALL"] + review.REVIEW_STATUSES)
        candidate_kind = st.selectbox("Candidate kind", _options(rows, "candidate_kind"))
        phase3_bundle_type = st.selectbox("Phase 3 bundle type", _options(rows, "phase3_bundle_type"))
        parser_ordered = st.selectbox("Parser ordered/not ordered", ["ALL", "true", "false"])
        parser_needs_review = st.selectbox("Parser needs review", ["ALL", "true", "false"])
        candidate_scope = st.selectbox("Strict versus broad candidates", ["ALL", "STRICT", "BROAD"])
        source_segment = st.selectbox("Source segment", _options(rows, "segment_name"))
        use_for_training_filter = st.selectbox("legacy use_for_training", ["ALL", "true", "false"])
        use_for_structure_filter = st.selectbox("use_for_structure_training", ["ALL", "true", "false"])
        use_for_portfolio_filter = st.selectbox("use_for_portfolio_effect_training", ["ALL", "true", "false"])
        portfolio_effect_filter = st.selectbox("portfolio_effect_status", ["ALL"] + review.PORTFOLIO_EFFECT_STATUSES)
        reviewed_filter = st.selectbox("Reviewed/unreviewed", ["ALL", "REVIEWED", "UNREVIEWED"])

    filtered = review.filter_candidates(
        rows,
        review_status=review_status,
        candidate_kind=candidate_kind,
        phase3_bundle_type=phase3_bundle_type,
        parser_ordered=parser_ordered,
        parser_needs_review=parser_needs_review,
        candidate_scope=candidate_scope,
        source_segment=source_segment,
        use_for_training=use_for_training_filter,
        use_for_structure_training=use_for_structure_filter,
        use_for_portfolio_effect_training=use_for_portfolio_filter,
        portfolio_effect_status=portfolio_effect_filter,
        reviewed=reviewed_filter,
    )

    metric_cols = st.columns(5)
    metric_cols[0].metric("Total", summary["total_candidates"])
    metric_cols[1].metric("Reviewed", summary["reviewed_candidates"])
    metric_cols[2].metric("Pending", summary["pending_candidates"])
    metric_cols[3].metric("Structure training", summary["structure_training_eligible_records"])
    metric_cols[4].metric("Progress", f"{summary['progress_percentage']}%")

    st.subheader("Decision Guidance")
    guidance_cols = st.columns(3)
    for index, (status, bullets) in enumerate(review.REVIEW_GUIDANCE.items()):
        with guidance_cols[index % 3]:
            st.markdown(f"**{status}:**")
            for bullet in bullets:
                st.write(f"- {bullet}")

    if not filtered:
        st.info("No candidates match the selected filters.")
        return

    table_rows = [
        {
            "candidate_id": row["candidate_id"],
            "source_row_number": row.get("source_row_number", ""),
            "segment": row.get("segment_name") or row.get("segment_id", ""),
            "candidate_kind": row.get("candidate_kind", ""),
            "phase3_bundle_type": row.get("phase3_bundle_type", ""),
            "review_bundle_type": row.get("current_review_bundle_type", ""),
            "phase3_is_ordered": row.get("phase3_is_ordered", ""),
            "review_status": row.get("current_review_status", ""),
            "structure_training": row.get("current_use_for_structure_training", ""),
            "portfolio_effect": row.get("current_portfolio_effect_status", ""),
        }
        for row in filtered
    ]
    st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)

    selected_id = st.selectbox(
        "Candidate",
        [row["candidate_id"] for row in filtered],
        format_func=lambda cid: next(
            f"{row['candidate_id']} | row {row.get('source_row_number', '')} | {row.get('candidate_kind', '')}"
            for row in filtered
            if row["candidate_id"] == cid
        ),
    )
    candidate = next(row for row in filtered if row["candidate_id"] == selected_id)
    current_decision = review.decision_for(candidate["candidate_id"], decisions)

    st.header(candidate["candidate_id"])
    c1, c2, c3 = st.columns(3)
    c1.write(f"**Source row number:** {candidate.get('source_row_number', '')}")
    c1.write(f"**Source message ID:** {candidate.get('source_message_id', '')}")
    c1.write(f"**Segment:** {candidate.get('segment_name') or candidate.get('segment_id', '')}")
    c2.write(f"**Candidate kind:** {candidate.get('candidate_kind', '')}")
    c2.write(f"**Phase 3 bundle type:** {candidate.get('phase3_bundle_type', '')}")
    c2.write(f"**Review bundle type:** {current_decision.get('review_bundle_type', '')}")
    c2.write(f"**Child count:** {candidate.get('phase3_child_count', '')}")
    c3.write(f"**Same-instrument identity result:** {candidate.get('same_instrument_identity', '')}")
    c3.write(f"**Actionable clause count:** {candidate.get('actionable_clause_count', '')}")
    c3.write(f"**Current review decision:** {current_decision.get('review_status', review.PENDING_REVIEW)}")
    c3.write(f"**Portfolio effect status:** {current_decision.get('portfolio_effect_status', review.NOT_EVALUATED)}")
    if current_decision.get("review_status") == review.CONFIRMED_ORDERED_REVERSAL:
        st.warning("CONFIRMED_ORDERED_REVERSAL is a deprecated legacy status. Use CONFIRMED_ORDERED_CLOSE_THEN_ENTRY for new text-level reviews.")

    st.subheader("Message Text")
    st.caption("Raw text")
    st.code(candidate.get("raw_text", ""))
    st.caption("Normalized text")
    st.code(candidate.get("normalized_text", ""))

    st.subheader("Structural Evidence")
    e1, e2, e3 = st.columns(3)
    with e1:
        _show_json("Detection reasons", candidate.get("detection_reasons", ""))
    with e2:
        _show_json("Detected separators", candidate.get("detected_separators", ""))
    with e3:
        _show_json("Additional clauses", candidate.get("extra_clauses_json", ""))

    clause_cols = st.columns(2)
    clause_cols[0].text_area("Clause 1", candidate.get("clause_1_text", ""), height=90, disabled=True)
    clause_cols[1].text_area("Clause 2", candidate.get("clause_2_text", ""), height=90, disabled=True)

    st.subheader("Parser Output")
    p1, p2 = st.columns(2)
    with p1:
        _show_json("Parser validation errors", candidate.get("phase3_validation_errors_json", ""))
        _show_json("Child 1 parsed JSON", candidate.get("child_1_json", ""))
    with p2:
        _show_json("Parser validation warnings", candidate.get("phase3_validation_warnings_json", ""))
        _show_json("Child 2 parsed JSON", candidate.get("child_2_json", ""))

    st.subheader("Review Decision")
    st.json(current_decision)

    status_index = review.REVIEW_STATUSES.index(current_decision.get("review_status", review.PENDING_REVIEW))
    selected_status = st.selectbox("Review status", review.REVIEW_STATUSES, index=status_index)
    effect_index = review.PORTFOLIO_EFFECT_STATUSES.index(
        current_decision.get("portfolio_effect_status", review.NOT_EVALUATED)
    )
    portfolio_effect_status = st.selectbox(
        "Portfolio effect status",
        review.PORTFOLIO_EFFECT_STATUSES,
        index=effect_index,
        help="For raw historical Phase 4 review this normally stays NOT_EVALUATED unless trustworthy position context is available.",
    )
    notes = st.text_area("Review notes", value=current_decision.get("review_notes", ""), height=90)
    use_for_structure_training = st.checkbox(
        "Use for structure training",
        value=current_decision.get("use_for_structure_training") == "true",
        help="Use only when close clause, entry clause, split, identity, and parsed labels are reliable.",
    )
    use_for_portfolio_effect_training = st.checkbox(
        "Use for portfolio-effect training",
        value=current_decision.get("use_for_portfolio_effect_training") == "true",
        help="Use only when trustworthy prior position context labels REVERSAL or SAME_SIDE_REENTRY.",
    )

    with st.expander("Corrections", expanded=False):
        corrected = {
            "corrected_candidate_kind": st.text_input("Corrected candidate kind", current_decision.get("corrected_candidate_kind", "")),
            "corrected_clause_1_text": st.text_area("Corrected clause 1 text", current_decision.get("corrected_clause_1_text", ""), height=80),
            "corrected_clause_2_text": st.text_area("Corrected clause 2 text", current_decision.get("corrected_clause_2_text", ""), height=80),
            "corrected_child_1_surface_instruction": st.text_input("Corrected child 1 surface instruction", current_decision.get("corrected_child_1_surface_instruction", "")),
            "corrected_child_2_surface_instruction": st.text_input("Corrected child 2 surface instruction", current_decision.get("corrected_child_2_surface_instruction", "")),
            "corrected_child_1_symbol": st.text_input("Corrected child 1 symbol", current_decision.get("corrected_child_1_symbol", "")),
            "corrected_child_2_symbol": st.text_input("Corrected child 2 symbol", current_decision.get("corrected_child_2_symbol", "")),
            "corrected_child_1_position_effect": st.text_input("Corrected child 1 position effect", current_decision.get("corrected_child_1_position_effect", "")),
            "corrected_child_2_position_effect": st.text_input("Corrected child 2 position effect", current_decision.get("corrected_child_2_position_effect", "")),
            "corrected_child_1_entry_capacity_pct": st.text_input("Corrected child 1 entry capacity pct", current_decision.get("corrected_child_1_entry_capacity_pct", "")),
            "corrected_child_2_entry_capacity_pct": st.text_input("Corrected child 2 entry capacity pct", current_decision.get("corrected_child_2_entry_capacity_pct", "")),
        }
        if st.button("Save Corrections"):
            _save(
                candidate,
                current_decision.get("review_status", review.PENDING_REVIEW),
                reviewer,
                notes,
                use_for_structure_training,
                use_for_portfolio_effect_training,
                portfolio_effect_status,
                corrected,
            )

    if st.button("Update Decision", type="primary"):
        _save(
            candidate,
            selected_status,
            reviewer,
            notes,
            use_for_structure_training,
            use_for_portfolio_effect_training,
            portfolio_effect_status,
            corrected={},
        )

    action_cols = st.columns(7)
    for col, label in zip(action_cols[:6], ACTION_TO_STATUS):
        with col:
            if st.button(label):
                _save(
                    candidate,
                    ACTION_TO_STATUS[label],
                    reviewer,
                    notes,
                    use_for_structure_training,
                    use_for_portfolio_effect_training,
                    portfolio_effect_status,
                    corrected={},
                )
    with action_cols[6]:
        if st.button("Clear Decision"):
            try:
                review.clear_decision(candidate["candidate_id"])
                review.write_review_reports()
                st.success("Decision cleared")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))


if __name__ == "__main__":
    main()
