INSERT INTO biz.asset_hacks (
    asset_id, defillama_id, name, technique, amount, returned_funds,
    chain, target_type, classification, bridge_hack, hack_date, source
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (asset_id, name, hack_date) DO UPDATE SET
    defillama_id = EXCLUDED.defillama_id,
    technique = EXCLUDED.technique,
    amount = EXCLUDED.amount,
    returned_funds = EXCLUDED.returned_funds,
    chain = EXCLUDED.chain,
    target_type = EXCLUDED.target_type,
    classification = EXCLUDED.classification,
    bridge_hack = EXCLUDED.bridge_hack,
    source = EXCLUDED.source,
    updated_at = NOW()
RETURNING id;
