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
#include <CopyTrader/CopyTrader_Common.mqh>
#include <CopyTrader/CopyTrader_HTTP.mqh>
#include <CopyTrader/CopyTrader_Signals.mqh>
#include <CopyTrader/CopyTrader_Trades.mqh>
#include <CopyTrader/CopyTrader_Pendings.mqh>

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
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

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   Print("MT5 CopyTrader EA stopped. Reason: ", reason);
}

//+------------------------------------------------------------------+
//| Trade transaction handler (fires on delete/fill/expire too)       |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
   // You said "any removal" should behave like cancel, so we just rescan
   if(CopyPendingOrders)
      CheckPendingChanges();

   // Optional: if you want position changes to react instantly too, uncomment:
   // CheckTradeChanges();
}

//+------------------------------------------------------------------+
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
{
   CheckTradeChanges();

   if(CopyPendingOrders)
      CheckPendingChanges();
}
