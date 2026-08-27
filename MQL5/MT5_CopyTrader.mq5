//+------------------------------------------------------------------+
//| MT5_CopyTrader.mq5                                               |
//| MT5 to cTrader Copy Trading EA                                   |
//+------------------------------------------------------------------+
#property copyright "Copyright 2025"
#property version   "1.15"
#property strict

input string BridgeServerURL   = "http://127.0.0.1:3140";
input int    RequestTimeout    = 2500;
input string MagicNumberFilter = "";
input bool   CopyPendingOrders = true;

// -------------------------------------------------------------------
// State synchronization / recovery
//
// Timer runs frequently, but full reconciliation is controlled by
// the intervals below. Existing Include modules remain responsible
// for the actual bridge payloads and HTTP posts.
// -------------------------------------------------------------------
input bool   EnablePeriodicStateSync      = true;
input int    StateSyncTimerIntervalSec    = 5;
input int    StateSyncFallbackIntervalSec = 60;
input int    StateSyncStartupRetrySec     = 10;
input int    StateSyncMaxStartupRetries   = 3;

#include <CopyTrader/CopyTrader_State.mqh>
#include <CopyTrader/CopyTrader_Common.mqh>
#include <CopyTrader/CopyTrader_HTTP.mqh>
#include <CopyTrader/CopyTrader_Signals.mqh>
#include <CopyTrader/CopyTrader_Trades.mqh>
#include <CopyTrader/CopyTrader_Pendings.mqh>


datetime g_lastStateSyncAt = 0;
datetime g_lastStartupRetryAt = 0;
int      g_startupSyncAttempts = 0;
bool     g_stateSyncInProgress = false;


//+------------------------------------------------------------------+
//| Execute a recovery/reconciliation pass.                          |
//|                                                                    |
//| Existing modules determine what is new/missing using their local |
//| state lists and post only relevant OPEN/PENDING events.           |
//+------------------------------------------------------------------+
void RunStateSync(const string reason)
{
   if(g_stateSyncInProgress)
   {
      PrintFormat(
         "State sync skipped: already in progress | reason=%s",
         reason
      );
      return;
   }

   g_stateSyncInProgress = true;

   PrintFormat(
      "State sync started | reason=%s | positions=%d | pending=%d",
      reason,
      PositionsTotal(),
      OrdersTotal()
   );

   // Existing open MT5 market positions.
   // Intended to recover positions missed while Python bridge was down.
   StartupSyncOpenTrades();

   // Existing MT5 pending orders.
   // First refreshes the baseline, then runs your existing pending
   // change detector to emit PENDING_OPEN events when needed.
   if(CopyPendingOrders)
   {
      UpdatePendingList();
      CheckPendingChanges();
   }

   g_lastStateSyncAt = TimeCurrent();

   PrintFormat(
      "State sync complete | reason=%s | tracked_positions=%d | tracked_pending=%d",
      reason,
      g_lastTradeCount,
      g_lastPendingCount
   );

   g_stateSyncInProgress = false;
}


//+------------------------------------------------------------------+
//| Expert initialization                                             |
//+------------------------------------------------------------------+
int OnInit()
{
   Print(
      "MT5 CopyTrader EA initialized. Bridge server: ",
      BridgeServerURL
   );

   if(RequestTimeout < 500)
   {
      PrintFormat(
         "WARNING RequestTimeout=%dms is very low; using a minimum practical timeout is recommended.",
         RequestTimeout
      );
   }

   if(StateSyncTimerIntervalSec < 1)
   {
      Print(
         "ERROR StateSyncTimerIntervalSec must be at least 1 second."
      );
      return(INIT_PARAMETERS_INCORRECT);
   }

   if(StateSyncFallbackIntervalSec < StateSyncTimerIntervalSec)
   {
      PrintFormat(
         "WARNING StateSyncFallbackIntervalSec=%d is below timer interval=%d; "
         "it will effectively run every timer tick.",
         StateSyncFallbackIntervalSec,
         StateSyncTimerIntervalSec
      );
   }

   if(!EventSetTimer(StateSyncTimerIntervalSec))
   {
      PrintFormat(
         "ERROR Failed to start EA timer. GetLastError=%d",
         GetLastError()
      );
      return(INIT_FAILED);
   }

   PrintFormat(
      "State sync configured | timer=%ds fallback=%ds startup_retry=%ds max_retries=%d HTTP_timeout=%dms",
      StateSyncTimerIntervalSec,
      StateSyncFallbackIntervalSec,
      StateSyncStartupRetrySec,
      StateSyncMaxStartupRetries,
      RequestTimeout
   );

   // Initial startup recovery. This is useful when both the EA and
   // Python bridge come up together. Timed retries below handle the
   // case where Python/cTrader is not ready yet.
   RunStateSync("ea_init");

   g_startupSyncAttempts = 1;
   g_lastStartupRetryAt = TimeCurrent();

   Print(
      "Initial positions tracked: ", g_lastTradeCount,
      ", pending tracked: ", g_lastPendingCount
   );

   return(INIT_SUCCEEDED);
}


//+------------------------------------------------------------------+
//| Expert deinitialization                                           |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();

   PrintFormat(
      "MT5 CopyTrader EA stopped. Reason: %d",
      reason
   );
}


//+------------------------------------------------------------------+
//| Timer: bounded startup retries plus periodic drift recovery.      |
//|                                                                    |
//| The timer itself is lightweight. It only calls existing scan/post |
//| logic on initial retries or when the fallback interval is due.    |
//+------------------------------------------------------------------+
void OnTimer()
{
   datetime now = TimeCurrent();

   // Startup retries make EA/bridge startup ordering less fragile.
   // Example: EA starts while main.py or cTrader auth is still loading.
   if(g_startupSyncAttempts < StateSyncMaxStartupRetries)
   {
      if(
         g_lastStartupRetryAt == 0 ||
         (now - g_lastStartupRetryAt) >= StateSyncStartupRetrySec
      )
      {
         g_startupSyncAttempts++;
         g_lastStartupRetryAt = now;

         RunStateSync(
            StringFormat(
               "startup_retry_%d_of_%d",
               g_startupSyncAttempts,
               StateSyncMaxStartupRetries
            )
         );
         return;
      }
   }

   if(!EnablePeriodicStateSync)
      return;

   bool fallbackDue =
      g_lastStateSyncAt == 0 ||
      (now - g_lastStateSyncAt) >= StateSyncFallbackIntervalSec;

   if(fallbackDue)
      RunStateSync("periodic_fallback");
}


//+------------------------------------------------------------------+
//| Trade transaction event                                           |
//+------------------------------------------------------------------+
void OnTradeTransaction(
   const MqlTradeTransaction &trans,
   const MqlTradeRequest &request,
   const MqlTradeResult &result
)
{
   PrintFormat(
      "DEBUG OnTradeTransaction: type=%d order=%I64u deal=%I64u symbol=%s order_type=%d order_state=%d",
      (int)trans.type,
      (ulong)trans.order,
      (ulong)trans.deal,
      trans.symbol,
      (int)trans.order_type,
      (int)trans.order_state
   );

   if(CopyPendingOrders)
   {
      Pendings_OnTradeTransaction(trans);

      // Backstop for unusual ordering or missing MT5 pending lifecycle
      // events. Normal operation should remain driven by the event itself.
      CheckPendingChanges();
   }
}


//+------------------------------------------------------------------+
//| Trade-list change event                                           |
//+------------------------------------------------------------------+
void OnTrade()
{
   if(CopyPendingOrders)
      CheckPendingChanges();
}


//+------------------------------------------------------------------+
//| Price tick event                                                  |
//+------------------------------------------------------------------+
void OnTick()
{
   // Normal real-time market position copy path.
   CheckTradeChanges();

   // Normal real-time pending order copy path.
   if(CopyPendingOrders)
      CheckPendingChanges();
}
