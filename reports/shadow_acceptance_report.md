# Shadow Acceptance Report

- Classification: `READY_FOR_MANUAL_SHADOW_USE`
- Interpreter: `C:\Astrodunia text parsing\trade_message_system\.venv\Scripts\python.exe`
- Python: `3.13.9`
- scikit-learn: `1.9.0`
- Git: `codex-shadow-acceptance` `1b8577054c5201ddc6a7175c953bb1de09208896`
- Database: `C:\Astrodunia text parsing\trade_message_system\storage\shadow_acceptance.db`
- Full suite: `132` passed, `0` failed, `0` skipped, `6` warnings

## Key Scenario Results

- First NIFTY reduction: `{'message_id': 'acceptance-001', 'before': {'verified': '100', 'shadow': None}, 'after_ingest': {'verified': '100', 'shadow': '50.0000000000'}, 'after_approve': {'verified': '50.0000000000', 'shadow': '50.0000000000'}}`
- Second NIFTY reduction: `{'message_id': 'acceptance-002', 'before': {'verified': '50.0000000000', 'shadow': '50.0000000000'}, 'after_ingest': {'verified': '50.0000000000', 'shadow': '25.0000000000'}, 'after_approve': {'verified': '25.0000000000', 'shadow': '25.0000000000'}}`
- Approval-time revalidation: `{'stale_ingestion_time_expected_value': '12.5000000000', 'latest_verified_before_original_approval': '20.0000000000', 'actual_approval_time_verified_value': '10.0000000000'}`
- Edit and approve: `{'service_result': {'status': 'APPROVED'}, 'verified_nvda': '37.5000000000', 'original_prediction_quantity_percent': '25', 'review': {'decision': 'APPROVED_WITH_CORRECTION', 'changed_fields_json': '{"quantity_percent": {"from": "25", "to": "50"}}'}, 'verified_event': {'source_message_id': 'acceptance-003', 'raw_text': 'PART PROFIT BOOK IN NVDA', 'normalized_text': 'PART PROFIT BOOK IN NVDA', 'record_type': 'TRADE_ACTION', 'ml_action': 'REDUCE_POSITION', 'ml_confidence': 0.4871564316729287, 'rule_action': 'REDUCE_POSITION', 'final_action': 'REDUCE_POSITION', 'resolution_source': 'RULE_AND_ML_AGREE', 'symbol_raw': 'NVDA', 'symbol': 'NVDA', 'symbols': ['NVDA'], 'market_group': 'Equity', 'contract_month': None, 'strike_price': None, 'option_type': None, 'direction': None, 'quantity_percent': '50', 'quantity_basis': 'CURRENT_HOLDING', 'remaining_holding_multiplier': '0.75', 'execution_prices': [], 'execution_price_primary': None, 'stop_loss': None, 'targets': [], 'is_correction': False, 'is_conditional': False, 'is_multi_instrument': False, 'requires_context': False, 'parent_source_message_id': None, 'child_event_index': None, 'related_source_message_id': None, 'supersedes_event_id': None, 'cancels_event_id': None, 'auto_apply_eligible': True, 'needs_review': False, 'validation_errors': [], 'validation_warnings': []}, 'label': {'was_corrected': 1}}`
- Rejection: `{'service_result': {'status': 'REJECTED'}, 'verified_amd': None, 'verified_event_count': 0, 'shadow_event_count': 1}`
- Idempotency: `{'duplicate_ingestion': {'status': 'DUPLICATE'}, 'duplicate_approval': {'status': 'ALREADY_REVIEWED'}, 'before': {'incoming_messages': 10, 'parser_predictions': 8, 'shadow_events': 6, 'verified_events': 7, 'human_reviews': 8, 'positions': 5}, 'after': {'incoming_messages': 10, 'parser_predictions': 8, 'shadow_events': 6, 'verified_events': 7, 'human_reviews': 8, 'positions': 5}}`
- Concurrent review: `{'results': [{'status': 'APPROVED'}, {'status': 'ALREADY_REVIEWED'}], 'event_count': 1, 'review_count': 1}`
- Restart persistence: `{'pending_ids': ['acceptance-restart'], 'details': True, 'positions': [{'portfolio_id': 'verified', 'symbol': 'NIFTY', 'current_allocation_pct': '10.0000000000'}, {'portfolio_id': 'verified', 'symbol': 'NVDA', 'current_allocation_pct': '37.5000000000'}, {'portfolio_id': 'shadow', 'symbol': 'NIFTY', 'current_allocation_pct': '10.0000000000'}, {'portfolio_id': 'shadow', 'symbol': 'NVDA', 'current_allocation_pct': '56.2500000000'}, {'portfolio_id': 'shadow', 'symbol': 'AMD', 'current_allocation_pct': '50'}, {'portfolio_id': 'verified', 'symbol': '', 'current_allocation_pct': '10'}]}`
- Streamlit smoke: `{'method': 'streamlit.testing.v1.AppTest', 'queue_loaded': True, 'button_count': 1, 'review_opened': True, 'before_counts': {'incoming_messages': 12, 'parser_predictions': 10, 'shadow_events': 6, 'verified_events': 8, 'human_reviews': 9, 'positions': 6}, 'after_counts': {'incoming_messages': 12, 'parser_predictions': 10, 'shadow_events': 6, 'verified_events': 8, 'human_reviews': 9, 'positions': 6}}`
- Export: `{'path': 'C:\\Astrodunia text parsing\\trade_message_system\\data\\verified_shadow_labels.csv', 'count': 9, 'decisions': ['APPROVED', 'APPROVED_WITH_CORRECTION', 'NEEDS_CONTEXT', 'NON_TRADE', 'REJECTED']}`
- Metrics: `{'total_reviews': 9, 'approval_rate': 0.6666666666666666, 'correction_rate': 0.4444444444444444, 'rejection_rate': 0.1111111111111111, 'manual_review_rate': 0.4444444444444444, 'complete_event_accuracy': 0.5555555555555556, 'action_accuracy': 0.6666666666666666, 'symbol_accuracy': 0.7777777777777778, 'percentage_accuracy': 0.6666666666666666, 'direction_accuracy': 0.7777777777777778, 'verified_shadow_position_differences': 3, 'metric_notes': {'complete_event_accuracy': 'Approximation: approved-as-parsed and not corrected, not a field-perfect equality across every event attribute.', 'manual_review_rate': 'Approximation over persisted human review decisions in the local shadow-review database.'}}`

## Defects Fixed
- 4 verified import / 2 migration foreign keys: Shadow migration and import/export tools were hard-wired to the default database, and import setup events lacked parent incoming_messages rows. Fix: Added explicit database path support and import source rows for verified setup events.. Test: tests/test_shadow_workflow.py::test_shadow_reduction_uses_verified_baseline_when_shadow_empty.
- 5 current-holding percentage workflow: Shadow reduction simulation could not reduce an imported verified position because the shadow portfolio was intentionally empty after import. Fix: Seeded a shadow-only baseline copy from verified holdings when applying safe reduction predictions to shadow.. Test: tests/test_shadow_workflow.py::test_shadow_reduction_uses_verified_baseline_when_shadow_empty.
- 14 human-reviewed data export: Rejected and needs-context decisions were not exported as human-reviewed training candidates, and export omitted required metadata. Fix: Recorded candidate labels for non-approved human decisions and expanded export columns.. Test: tests/test_shadow_workflow.py::test_rejected_and_needs_context_reviews_export_as_training_candidates.

## Historical Isolation

- Changed: `False`
- Baseline count/hash: `0` / `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945`
- Final count/hash: `0` / `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945`

## Unresolved Issues

- None.
