//+------------------------------------------------------------------+
//|                                           MT5_CopyTrader.mq5     |
//|                                  MT5 to cTrader Copy Trading EA  |
//+------------------------------------------------------------------+
#property copyright "Copyright 2025"
#property version   "1.09"
#property strict

// Inputs
input string BridgeServerURL   = "http://127.0.0.1:3140";
input int    RequestTimeout    = 5000;
input string MagicNumberFilter = "";
input bool   CopyPendingOrders = true;

#include <CopyTrader/CopyTrader_State.mqh>
#include <CopyTrader/CopyTrader_HTTP.mqh>
#include <CopyTrader/CopyTrader_Signals.mqh>
#include <CopyTrader/CopyTrader_Trades.mqh>
#include <CopyTrader/CopyTrader_Pendings.mqh>   // <-- add this

int OnInit()
{
   Print("MT5 CopyTrader EA initialized. Bridge server: ", BridgeServerURL);

   UpdateTradeList();
   UpdatePendingList();

   Print("Initial positions tracked: ", g_lastTradeCount,
         ", pending tracked: ", g_lastPendingCount);

   // Catch existing pending orders immediately (no need to wait for tick)
   if(CopyPendingOrders)
      CheckPendingChanges();

   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   Print("MT5 CopyTrader EA stopped. Reason: ", reason);
}

void OnTick()
{
   CheckTradeChanges();

   if(CopyPendingOrders)
      CheckPendingChanges();
}
