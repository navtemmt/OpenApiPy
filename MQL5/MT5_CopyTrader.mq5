//+------------------------------------------------------------------+
//| MT5_CopyTrader.mq5                                               |
//| MT5 to cTrader Copy Trading EA                                   |
//+------------------------------------------------------------------+
#property copyright "Copyright 2025"
#property version   "1.13"
#property strict

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

int OnInit()
{
   Print("MT5 CopyTrader EA initialized. Bridge server: ", BridgeServerURL);

   UpdateTradeList();
   UpdatePendingList();

   Print("Initial positions tracked: ", g_lastTradeCount,
         ", pending tracked: ", g_lastPendingCount);

   // Startup sync: discover any existing pendings and send PENDING_OPEN once
   if(CopyPendingOrders)
      CheckPendingChanges();

   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   Print("MT5 CopyTrader EA stopped. Reason: ", reason);
}

void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
   // TradeTransaction event handler for EAs. [web:17]
   PrintFormat("DEBUG OnTradeTransaction: type=%d order=%I64u deal=%I64u symbol=%s order_type=%d order_state=%d",
               (int)trans.type, (ulong)trans.order, (ulong)trans.deal,
               trans.symbol, (int)trans.order_type, (int)trans.order_state);

   if(CopyPendingOrders)
   {
      Pendings_OnTradeTransaction(trans);

      // Backstop: after any trade transaction, reconcile pendings immediately
      // (helps when some delete events are missed or arrive oddly).
      CheckPendingChanges();
   }
}

void OnTrade()
{
   if(CopyPendingOrders)
      CheckPendingChanges();
}

void OnTick()
{
   CheckTradeChanges();

   // Optional backstop
   if(CopyPendingOrders)
      CheckPendingChanges();
}
