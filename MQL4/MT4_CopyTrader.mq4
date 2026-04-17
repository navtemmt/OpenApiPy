//+------------------------------------------------------------------+
//| MT4_CopyTrader.mq4                                               |
//| MT4 to cTrader Copy Trading EA                                   |
//+------------------------------------------------------------------+
#property copyright "Copyright 2025"
#property version   "1.14"
#property strict

input string BridgeServerURL   = "http://127.0.0.1:3140";
input int    RequestTimeout    = 5000;
input string MagicNumberFilter = "";
input bool   CopyPendingOrders = true;

#include "CopyTrader_State.mqh"
#include "CopyTrader_Common.mqh"
#include "CopyTrader_HTTP.mqh"
#include "CopyTrader_Signals.mqh"
#include "CopyTrader_Trades.mqh"
#include "CopyTrader_Pendings.mqh"

int OnInit()
{
   Print("MT4 CopyTrader EA initialized. Bridge server: ", BridgeServerURL);

   // Startup sync existing live market orders first.
   // This recovers missed/reverse positions after EA restart/reinit.
   StartupSyncOpenTrades();

   // Build baseline for pending orders, then discover any existing pendings once.
   UpdatePendingList();

   Print("Initial trades tracked: ", g_lastTradeCount,
         ", pending tracked: ", g_lastPendingCount);

   // Startup sync: discover any existing pendings and send PENDING_OPEN once
   if(CopyPendingOrders)
      CheckPendingChanges();

   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   Print("MT4 CopyTrader EA stopped. Reason: ", reason);
}

void OnTick()
{
   CheckTradeChanges();

   if(CopyPendingOrders)
      CheckPendingChanges();
}
