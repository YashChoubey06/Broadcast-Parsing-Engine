import pandas as pd
from pathlib import Path
from src.config import REPORTS_DIR

def process_disagreements():
    f = REPORTS_DIR / "dataset_parser_disagreements.csv"
    if not f.exists(): return
    df = pd.read_csv(f)
    
    # 4. Audit dataset/parser disagreements
    
    # disagreements by supplied action label
    by_ds = df['dataset_action'].value_counts().reset_index()
    by_ds.columns = ['action', 'count']
    
    # disagreements by parser action
    by_parser = df['parser_action'].value_counts().reset_index()
    by_parser.columns = ['action', 'count']
    
    # disagreements by symbol
    by_sym = df['dataset_symbol'].value_counts().reset_index()
    by_sym.columns = ['symbol', 'count']
    
    # caused by missing symbol
    missing_sym = len(df[df['parser_symbol'].isna() | (df['parser_symbol'] == '')])
    
    # percent interpretation
    # naive: if raw text contains %
    pct_interp = len(df[df['raw_text'].str.contains('%', na=False)])
    
    # sell ambiguity
    sell_ambig = len(df[df['dataset_action'].isin(['OPEN_SHORT', 'REDUCE_POSITION', 'CLOSE_POSITION']) & df['parser_action'].isin(['OPEN_SHORT', 'REDUCE_POSITION', 'CLOSE_POSITION'])])
    
    # correction / conditional
    correction = len(df[df['raw_text'].str.contains('correction|ignore|conditional', case=False, na=False)])
    
    summary = pd.DataFrame([{
        "total": len(df),
        "missing_symbol": missing_sym,
        "percentage_interpretation": pct_interp,
        "sell_ambiguity": sell_ambig,
        "corrections_conditionals": correction
    }])
    summary.to_csv(REPORTS_DIR / "disagreement_summary.csv", index=False)
    
    samples = df.head(50)
    samples.to_csv(REPORTS_DIR / "disagreement_samples.csv", index=False)

def process_review_queue():
    f = REPORTS_DIR / "manual_review_queue.csv"
    if not f.exists(): return
    df = pd.read_csv(f)
    
    # 5. Audit the manual-review queue
    def normalize_reason(r):
        r = str(r).upper()
        if "MISSING_PRIOR_POSITION" in r: return "MISSING_PRIOR_POSITION"
        if "DATASET_PARSER_DISAGREEMENT" in r: return "DATASET_PARSER_DISAGREEMENT"
        if "VALIDATION_FAILED_OR_LOW_CONFIDENCE" in r or "LOW_CONFIDENCE" in r: return "LOW_CONFIDENCE"
        if "CONDITIONAL" in r: return "CONDITIONAL_INSTRUCTION"
        if "CORRECTION" in r: return "CORRECTION_REQUIRES_CONTEXT"
        if "CANCEL_PREVIOUS" in r: return "CANCEL_PREVIOUS_REQUIRES_CONTEXT"
        if "MULTI_INSTRUMENT" in r: return "MULTI_INSTRUMENT"
        if "OPPOSITE_DIRECTION" in r: return "OPPOSITE_DIRECTION_CONFLICT"
        if "MISSING_SYMBOL" in r: return "MISSING_SYMBOL"
        if "MISSING_ENTRY" in r: return "MISSING_ENTRY_PRICE"
        if "PLAIN_BUY" in r or "ADD_LONG" in r: return "PLAIN_BUY_ON_EXISTING_POSITION"
        if "AMBIGUOUS" in r: return "AMBIGUOUS_SELL"
        if "GROUP" in r: return "GROUP_ACTION"
        return "OTHER"
        
    df['normalised_review_reason'] = df['review_reason'].apply(normalize_reason)
    
    counts = df['normalised_review_reason'].value_counts().reset_index()
    counts.columns = ['normalised_review_reason', 'count']
    counts['percentage'] = (counts['count'] / 1043.0) * 100
    
    def requires_context(r):
        if r in ["MISSING_PRIOR_POSITION", "CORRECTION_REQUIRES_CONTEXT", "CANCEL_PREVIOUS_REQUIRES_CONTEXT"]: return True
        return False
        
    def parser_fixable(r):
        if r in ["DATASET_PARSER_DISAGREEMENT", "LOW_CONFIDENCE", "MISSING_SYMBOL", "AMBIGUOUS_SELL"]: return True
        return False
        
    counts['requires_genuine_context'] = counts['normalised_review_reason'].apply(requires_context)
    counts['fixed_through_better_parsing'] = counts['normalised_review_reason'].apply(parser_fixable)
    
    counts.to_csv(REPORTS_DIR / "manual_review_reason_counts.csv", index=False)
    
    # Samples
    samples = df.groupby('normalised_review_reason').apply(lambda x: x.head(10)).reset_index(drop=True)
    samples.to_csv(REPORTS_DIR / "manual_review_samples.csv", index=False)

def process_final_holdings():
    # 8. Audit final holdings
    f = REPORTS_DIR / "final_holdings.csv"
    ev_f = REPORTS_DIR / "trade_event_ledger.csv"
    if not f.exists() or not ev_f.exists(): return
    
    df = pd.read_csv(f)
    ev_df = pd.read_csv(ev_f)
    
    audit_rows = []
    
    for _, row in df.iterrows():
        sym = row['symbol']
        direction = row['direction']
        alloc = float(row['current_allocation_pct'])
        
        # get events
        sym_events = ev_df[(ev_df['symbol'] == sym) & (ev_df['direction'] == direction)].sort_values('processing_order')
        num_events = len(sym_events)
        first_event = sym_events.iloc[0]['final_action'] if num_events > 0 else "N/A"
        last_event = sym_events.iloc[-1]['final_action'] if num_events > 0 else "N/A"
        
        flags = []
        if alloc < 0: flags.append("NEGATIVE_ALLOCATION")
        if alloc > 100: flags.append("ALLOCATION_ABOVE_MAX")
        if pd.isna(sym) or sym == "": flags.append("MISSING_SYMBOL")
        if row['status'] == "CLOSED" and alloc != 0: flags.append("CLOSED_WITH_NONZERO_ALLOCATION")
        if row['status'] == "OPEN" and alloc == 0: flags.append("OPEN_WITH_ZERO_ALLOCATION")
        
        # chronological checks
        if num_events > 0:
            if sym_events.iloc[0]['final_action'] not in ["OPEN_LONG", "OPEN_SHORT"]:
                flags.append("UPDATE_BEFORE_OPEN") # Reduction or update before open
            
        audit_rows.append({
            "portfolio_id": row.get('portfolio_id', 'default'),
            "symbol": sym,
            "market": row.get('market', ''),
            "direction": direction,
            "contract_month": row.get('contract_month', ''),
            "option_type": row.get('option_type', ''),
            "strike": row.get('strike_price', ''),
            "current_allocation": alloc,
            "average_entry": row.get('average_entry', ''),
            "stop_loss": row.get('stop_loss', ''),
            "status": row['status'],
            "number_of_applied_events": num_events,
            "first_applied_event": first_event,
            "last_applied_event": last_event,
            "flags": " | ".join(flags)
        })
        
    pd.DataFrame(audit_rows).to_csv(REPORTS_DIR / "final_holdings_audit.csv", index=False)

def process_timelines():
    # 9. Produce symbol timelines
    ev_f = REPORTS_DIR / "trade_event_ledger.csv"
    if not ev_f.exists(): return
    ev_df = pd.read_csv(ev_f)
    
    symbols = ['NIFTY', 'BANKNIFTY', 'GOLD', 'SILVER', 'CRUDE', 'NATURAL_GAS', 'NVDA', 'AMD', 'INFY', 'COPPER']
    
    tls = ev_df[ev_df['symbol'].isin(symbols)].sort_values(['symbol', 'processing_order']).copy()
    
    cols = [
        'processing_order', 'source_message_id', 'created_at', 'raw_text', 'final_action',
        'quantity_percent', 'quantity_basis', 'allocation_before', 'allocation_after',
        'direction', 'execution_price_primary', 'stop_loss', 'record_type'
    ]
    # some columns might not exist if sqlite schemas changed, safe get
    exist_cols = [c for c in cols if c in tls.columns]
    
    tls[exist_cols].to_csv(REPORTS_DIR / "selected_symbol_timelines.csv", index=False)
    

def compare_dry_run_and_safe_apply():
    # 7. Compare dry-run and safe-apply results
    safe_f = REPORTS_DIR / "final_holdings.csv"
    dry_f = REPORTS_DIR / "replay_dry_run_final_holdings.csv"
    if not safe_f.exists() or not dry_f.exists(): return
    
    safe_df = pd.read_csv(safe_f).fillna('')
    dry_df = pd.read_csv(dry_f).fillna('')
    
    # Sort them similarly so we can compare
    sort_cols = ['symbol', 'direction', 'average_entry_price', 'stop_loss', 'status']
    safe_df = safe_df.sort_values(by=sort_cols).reset_index(drop=True)
    dry_df = dry_df.sort_values(by=sort_cols).reset_index(drop=True)
    
    diffs = []
    
    # Simple row-by-row comparison since they should be identical sets
    for i in range(max(len(safe_df), len(dry_df))):
        s_row = safe_df.iloc[i].to_dict() if i < len(safe_df) else None
        d_row = dry_df.iloc[i].to_dict() if i < len(dry_df) else None
        
        if s_row != d_row:
            diffs.append({
                "safe_row": s_row,
                "dry_row": d_row
            })
            
    pd.DataFrame(diffs).to_csv(REPORTS_DIR / "dry_run_safe_apply_comparison.csv", index=False)


if __name__ == "__main__":
    process_disagreements()
    process_review_queue()
    process_final_holdings()
    process_timelines()
    compare_dry_run_and_safe_apply()
    print("audit_data.py completed.")
