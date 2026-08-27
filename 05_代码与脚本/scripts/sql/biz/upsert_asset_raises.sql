INSERT INTO biz.asset_raises (
    asset_id, defillama_id, protocol_name, round, raise_date,
    amount, chains, sector, category, lead_investors, other_investors,
    valuation, source
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (asset_id, round, raise_date) DO UPDATE SET
    defillama_id = EXCLUDED.defillama_id,
    protocol_name = EXCLUDED.protocol_name,
    amount = EXCLUDED.amount,
    chains = EXCLUDED.chains,
    sector = EXCLUDED.sector,
    category = EXCLUDED.category,
    lead_investors = EXCLUDED.lead_investors,
    other_investors = EXCLUDED.other_investors,
    valuation = EXCLUDED.valuation,
    source = EXCLUDED.source,
    updated_at = NOW()
RETURNING id;
