-- The free tier is deliberately tiny so a curl loop reaches 429 in seconds.
INSERT INTO tiers (name, per_minute, burst) VALUES
 ('free', 5, 5),
 ('basic', 60, 90),
 ('pro', 600, 1200)
ON CONFLICT (name) DO UPDATE
  SET per_minute = EXCLUDED.per_minute, burst = EXCLUDED.burst;

-- Fixed, memorable keys, so the app is usable the moment it boots.
INSERT INTO api_keys (api_key, owner, tier, active) VALUES
 ('k_demo_free',     'demo-student',   'free',  TRUE),
 ('k_demo_basic',    'demo-startup',   'basic', TRUE),
 ('k_demo_pro',      'demo-enterprise','pro',   TRUE),
 ('k_demo_disabled', 'ex-customer',    'basic', FALSE)
ON CONFLICT DO NOTHING;

-- A day of history so /usage has something to show on first run.
INSERT INTO requests (api_key, resource, status, algorithm, at)
SELECT k.api_key,
       (ARRAY['time','quote','echo'])[1 + (g % 3)],
       CASE WHEN g % 11 = 0 THEN 429 ELSE 200 END,
       CASE WHEN g % 2 = 0 THEN 'sliding' ELSE 'fixed' END,
       now() - (g || ' minutes')::interval
FROM api_keys k, generate_series(1, 25) AS g
WHERE k.active
  AND NOT EXISTS (SELECT 1 FROM requests r WHERE r.api_key = k.api_key);
