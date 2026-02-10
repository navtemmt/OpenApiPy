#pragma once
#property strict

// Input parameters (declared in EA, referenced by included code)
input string BridgeServerURL   = "http://127.0.0.1:3140";
input int    RequestTimeout    = 5000;
input string MagicNumberFilter = "";
input bool   CopyPendingOrders = true; // still unused in v1.04
