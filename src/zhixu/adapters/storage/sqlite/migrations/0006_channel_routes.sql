CREATE TABLE channel_routes (
    channel TEXT NOT NULL,
    channel_account TEXT NOT NULL,
    opaque_ref TEXT NOT NULL,
    route_kind TEXT NOT NULL CHECK (
        route_kind IN ('private','group','channel','actor')
    ),
    commands_enabled INTEGER NOT NULL DEFAULT 0 CHECK (commands_enabled IN (0,1)),
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(channel,channel_account,opaque_ref)
);

CREATE INDEX channel_routes_account
ON channel_routes(channel,channel_account,route_kind,last_seen_at);
