//+------------------------------------------------------------------+
//| MT5_CopyTrader.mq5                                               |
//| MT5 to cTrader Copy Trading EA                                   |
//+------------------------------------------------------------------+
#property copyright "Copyright 2025"
#property version   "1.17"
#property strict

input string BridgeServerURL   = "http://127.0.0.1:3140";
input int    RequestTimeout    = 2500;
input string MagicNumberFilter = "";
input bool   CopyPendingOrders = true;

// -------------------------------------------------------------------
// Bridge availability / startup sync
// -------------------------------------------------------------------
input int    BridgeHealthCheckIntervalSec = 15;
input int    BridgeDownAfterFailures      = 2;

#include <CopyTrader/CopyTrader_State.mqh>
#include <CopyTrader/CopyTrader_Common.mqh>
#include <CopyTrader/CopyTrader_HTTP.mqh>
#include <CopyTrader/CopyTrader_Signals.mqh>
#include <CopyTrader/CopyTrader_Trades.mqh>
#include <CopyTrader/CopyTrader_Pendings.mqh>

bool     g_stateSyncInProgress     = false;
bool     g_bridgeWasAvailable      = false;
int      g_bridgeHealthFailCount   = 0;
datetime g_lastBridgeHealthCheckAt = 0;


//+------------------------------------------------------------------+
//| Notify bridge that startup snapshot is complete                  |
//+------------------------------------------------------------------+
void SendSyncComplete(const string reason)
{
   string json = "{";
   json += "\"event_type\":\"SYNC_COMPLETE\",";
   json += "\"startupSync\":true,";
   json += "\"startupRecovery\":true,";
   json += "\"syncOrigin\":\"startup\",";
   json += "\"reason\":\"" + JsonEscape(reason) + "\"";
   json += "}";

   SendToServer(json);
}


//+------------------------------------------------------------------+
//| Execute a full recovery snapshot                                 |
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

   StartupSyncOpenTrades();

   if(CopyPendingOrders)
      StartupSyncPendingOrders("startup");

   SendSyncComplete(reason);

   PrintFormat(
      "State sync complete | reason=%s | tracked_positions=%d | tracked_pending=%d",
      reason,
      g_lastTradeCount,
      g_lastPendingCount
   );

   g_stateSyncInProgress = false;
}


//+------------------------------------------------------------------+
//| Check whether bridge currently requests a resync                 |
//+------------------------------------------------------------------+
bool BridgeRequestsSyncNow()
{
   if(!BridgeHealthCheck())
   {
      g_bridgeHealthFailCount++;

      if(g_bridgeHealthFailCount >= BridgeDownAfterFailures)
      {
         if(g_bridgeWasAvailable)
         {
            PrintFormat(
               "Bridge marked unavailable after %d failed health checks",
               g_bridgeHealthFailCount
            );
         }
         g_bridgeWasAvailable = false;
      }

      PrintFormat(
         "Bridge health check failed | failures=%d | status=%d | error=%s",
         g_bridgeHealthFailCount,
         BridgeLastStatusCode(),
         BridgeLastError()
      );
      return false;
   }

   bool recovered = !g_bridgeWasAvailable && g_bridgeHealthFailCount >= BridgeDownAfterFailures;

   g_bridgeWasAvailable = true;
   g_bridgeHealthFailCount = 0;

   // Verbose health logging removed to avoid spam; only log state changes.
   // PrintFormat(
   //    "Bridge health ok | sync_required=%s | body=%s",
   //    BridgeSyncRequired() ? "true" : "false",
   //    BridgeLastResponseBody()
   // );

   if(recovered)
      Print("Bridge recovered after being unavailable");

   return BridgeSyncRequired();
}


//+------------------------------------------------------------------+
//| Expert initialization                                            |
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

   if(BridgeHealthCheckIntervalSec < 1)
   {
      Print("ERROR BridgeHealthCheckIntervalSec must be at least 1 second.");
      return(INIT_PARAMETERS_INCORRECT);
   }

   if(BridgeDownAfterFailures < 1)
   {
      Print("ERROR BridgeDownAfterFailures must be at least 1.");
      return(INIT_PARAMETERS_INCORRECT);
   }

   if(!EventSetTimer(BridgeHealthCheckIntervalSec))
   {
      PrintFormat(
         "ERROR Failed to start EA timer. GetLastError=%d",
         GetLastError()
      );
      return(INIT_FAILED);
   }

   PrintFormat(
      "Bridge health configured | interval=%ds down_after_failures=%d HTTP_timeout=%dms",
      BridgeHealthCheckIntervalSec,
      BridgeDownAfterFailures,
      RequestTimeout
   );

   RunStateSync("mt5_restart");

   g_lastBridgeHealthCheckAt = TimeCurrent();

   Print(
      "Initial positions tracked: ", g_lastTradeCount,
      ", pending tracked: ", g_lastPendingCount
   );

   return(INIT_SUCCEEDED);
}


//+------------------------------------------------------------------+
//| Expert deinitialization                                          |
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
//| Timer: detect bridge recovery and bridge-requested sync          |
//+------------------------------------------------------------------+
void OnTimer()
{
   datetime now = TimeCurrent();

   if(
      g_lastBridgeHealthCheckAt != 0 &&
      (now - g_lastBridgeHealthCheckAt) < BridgeHealthCheckIntervalSec
   )
   {
      return;
   }

   g_lastBridgeHealthCheckAt = now;

   if(BridgeRequestsSyncNow())
      RunStateSync("bridge_restart");
}


//+------------------------------------------------------------------+
//| Trade transaction event                                          |
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
      CheckPendingChanges();
   }
}


//+------------------------------------------------------------------+
//| Trade-list change event                                          |
//+------------------------------------------------------------------+
void OnTrade()
{
   if(CopyPendingOrders)
      CheckPendingChanges();
}


//+------------------------------------------------------------------+
//| Price tick event                                                 |
//+------------------------------------------------------------------+
void OnTick()
{
   CheckTradeChanges();

   if(CopyPendingOrders)
      CheckPendingChanges();
}
