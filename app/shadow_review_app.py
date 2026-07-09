import streamlit as st
import pandas as pd
import json
import sys
import os
from pathlib import Path
from decimal import Decimal

# Ensure src is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.database import get_connection
from src.config import DATABASE_PATH
from src.schemas import ParsedMessageBundle, ParsedTradeEvent
import src.shadow_service as shadow_service

st.set_page_config(page_title="Shadow Testing & Human Review", layout="wide")

ACTION_OPTIONS = [
    "UNKNOWN",
    "OPEN_LONG",
    "OPEN_SHORT",
    "ADD_LONG",
    "ADD_SHORT",
    "REDUCE_POSITION",
    "CLOSE_POSITION",
    "HOLD_POSITION",
    "UPDATE_STOP_LOSS",
    "TARGET_HIT",
    "NON_TRADE",
    "AMBIGUOUS",
    "CONDITIONAL_INSTRUCTION",
    "CANCEL_PREVIOUS",
]

DIRECTION_OPTIONS = ["LONG", "SHORT", "UNKNOWN"]


def parse_prediction(prediction_json):
    data = json.loads(prediction_json)
    if "child_events" in data:
        return ParsedMessageBundle.from_json(prediction_json)
    return ParsedTradeEvent.from_json(prediction_json)


def first_event(prediction):
    if isinstance(prediction, ParsedMessageBundle):
        if prediction.child_events:
            return prediction.child_events[0]
        return ParsedTradeEvent(source_message_id=prediction.source_message_id, raw_text=prediction.raw_text)
    return prediction


def option_index(options, value, default=0):
    return options.index(value) if value in options else default

def fetch_pending():
    return shadow_service.get_pending_reviews(DATABASE_PATH)

def fetch_review_details(msg_id):
    return shadow_service.get_review_details(msg_id, DATABASE_PATH)

def get_positions(portfolio_id):
    with get_connection(DATABASE_PATH) as conn:
        cur = conn.execute("SELECT * FROM positions WHERE portfolio_id=? AND status='OPEN'", (portfolio_id,))
        return [dict(row) for row in cur.fetchall()]

def render_diff(verified_df, shadow_df):
    if verified_df.empty and shadow_df.empty:
        return pd.DataFrame()
        
    merged = pd.merge(
        verified_df, shadow_df, 
        on=['symbol', 'direction'], 
        how='outer', 
        suffixes=('_ver', '_sha')
    ).fillna('0')
    
    return merged[['symbol', 'direction', 'current_allocation_pct_ver', 'current_allocation_pct_sha']]

def execute_review_action(action, msg_id, **kwargs):
    try:
        if action == "approve":
            res = shadow_service.approve_as_parsed(msg_id, st.session_state.reviewer)
        elif action == "edit_and_approve":
            res = shadow_service.edit_and_approve(msg_id, st.session_state.reviewer, kwargs['corrected_event'], kwargs['changed_fields'], kwargs['notes'])
        elif action == "reject":
            res = shadow_service.reject(msg_id, st.session_state.reviewer, kwargs['notes'])
        elif action == "non_trade":
            res = shadow_service.mark_non_trade(msg_id, st.session_state.reviewer, kwargs['notes'])
        elif action == "needs_context":
            res = shadow_service.mark_needs_context(msg_id, st.session_state.reviewer, kwargs['notes'])
        elif action == "duplicate":
            res = shadow_service.mark_duplicate(msg_id, st.session_state.reviewer, kwargs['notes'])
        else:
            return
            
        if res["status"] in ["APPROVED", "REJECTED", "NON_TRADE", "NEEDS_CONTEXT", "IGNORED"]:
            st.success(f"Review saved as {res['status']}")
            st.session_state.selected_msg = None
            st.rerun()
        else:
            st.error(f"Error: {res.get('status', 'Unknown')} - {res.get('message', '')}")
    except Exception as e:
        st.error(f"Execution Error: {e}")

def main():
    st.title("Trade Message Shadow Testing")
    
    if "reviewer" not in st.session_state:
        st.session_state.reviewer = "Human1"
    
    with st.sidebar:
        st.header("Review Queue")
        st.session_state.reviewer = st.text_input("Reviewer ID", st.session_state.reviewer)
        pending = fetch_pending()
        st.write(f"Pending Items: {len(pending)}")
        
        selected = None
        for p in pending:
            label = f"{p['source_message_id']} - {p['segment_name']}"
            if st.button(label, key=f"btn_{p['source_message_id']}"):
                selected = p['source_message_id']
                st.session_state.selected_msg = selected
                st.rerun()

    if "selected_msg" not in st.session_state or not st.session_state.selected_msg:
        st.info("Select a pending message from the sidebar.")
        
        st.header("Global Holdings Status")
        v_pos = get_positions("verified")
        s_pos = get_positions("shadow")
        
        v_df = pd.DataFrame(v_pos) if v_pos else pd.DataFrame(columns=['symbol', 'direction', 'current_allocation_pct'])
        s_df = pd.DataFrame(s_pos) if s_pos else pd.DataFrame(columns=['symbol', 'direction', 'current_allocation_pct'])
        
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Verified Portfolio")
            st.dataframe(v_df[['symbol', 'direction', 'current_allocation_pct', 'status']] if not v_df.empty else v_df)
        with col2:
            st.subheader("Shadow Portfolio")
            st.dataframe(s_df[['symbol', 'direction', 'current_allocation_pct', 'status']] if not s_df.empty else s_df)
            
        return

    msg_id = st.session_state.selected_msg
    details = fetch_review_details(msg_id)
    if not details:
        st.warning("Message no longer found or already reviewed.")
        st.session_state.selected_msg = None
        if st.button("Refresh"): st.rerun()
        return

    pred_json = details["prediction_json"]
    prediction = parse_prediction(pred_json)
    event = first_event(prediction)
    validation_status = details["validation_status"]
    can_approve_as_parsed = validation_status == "VALID"

    st.header(f"Review: {msg_id}")
    st.subheader("Raw Message")
    st.code(details["raw_text"])
    
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Parser Prediction")
        st.json(prediction.to_dict())
        
    with col2:
        st.subheader("Context & Status")
        st.write(f"**Segment:** {details['segment_name']}")
        st.write(f"**Validation Status:** {validation_status}")
        if not can_approve_as_parsed:
            st.warning(
                "This prediction is INVALID and cannot be approved as parsed. "
                "Use Edit & Approve, Needs Context, or Reject."
            )
        if isinstance(prediction, ParsedMessageBundle):
            st.write(f"**Bundle Type:** {prediction.bundle_type}")
            st.write(f"**Ordered:** {prediction.is_ordered}")
            st.write(f"**Confidence:** {event.ml_confidence}")
        else:
            st.write(f"**Confidence:** {event.ml_confidence}")

    st.divider()
    
    st.subheader("Edit & Approve Form")
    notes = st.text_input("Review Notes (Optional)")
    
    with st.expander("Manual Correction Form", expanded=False):
        selected_child_index = 0
        if isinstance(prediction, ParsedMessageBundle) and prediction.child_events:
            selected_child_index = st.selectbox(
                "Child Event",
                list(range(len(prediction.child_events))),
                format_func=lambda i: f"Child {i + 1}: {prediction.child_events[i].final_action}",
            )
            event = prediction.child_events[selected_child_index]

        c_action = st.selectbox(
            "Action",
            ACTION_OPTIONS,
            index=option_index(ACTION_OPTIONS, event.final_action),
        )
        c_symbol = st.text_input("Symbol", event.symbol or "")
        c_dir = st.selectbox(
            "Direction",
            DIRECTION_OPTIONS,
            index=option_index(DIRECTION_OPTIONS, event.direction, default=2),
        )
        c_qty = st.text_input("Quantity %", str(event.quantity_percent) if event.quantity_percent else "")
        
        if st.button("Apply Manual Correction"):
            try:
                if isinstance(prediction, ParsedMessageBundle):
                    corrected = ParsedMessageBundle.from_json(prediction.to_json())
                    new_event = corrected.child_events[selected_child_index]
                    changed_fields = {f"child_{selected_child_index + 1}": {"action": c_action, "symbol": c_symbol, "qty": c_qty}}
                else:
                    corrected = ParsedTradeEvent.from_json(event.to_json())
                    new_event = corrected
                    changed_fields = {"action": c_action, "symbol": c_symbol, "qty": c_qty}

                new_event.final_action = c_action
                new_event.symbol = c_symbol
                new_event.direction = c_dir
                new_event.quantity_percent = Decimal(c_qty) if c_qty else None
                execute_review_action("edit_and_approve", msg_id, corrected_event=corrected, changed_fields=changed_fields, notes=notes)
            except Exception as e:
                st.error(f"Correction Error: {e}")

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        if st.button("Approve as Parsed", type="primary", disabled=not can_approve_as_parsed):
            execute_review_action("approve", msg_id)
    with col2:
        if st.button("Reject"):
            execute_review_action("reject", msg_id, notes=notes)
    with col3:
        if st.button("Mark Non-Trade"):
            execute_review_action("non_trade", msg_id, notes=notes)
    with col4:
        if st.button("Needs Context"):
            execute_review_action("needs_context", msg_id, notes=notes)
    with col5:
        if st.button("Duplicate"):
            execute_review_action("duplicate", msg_id, notes=notes)

if __name__ == "__main__":
    main()
