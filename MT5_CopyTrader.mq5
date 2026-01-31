//+------------------------------------------------------------------+
//|                                           MT5_CopyTrader.mq5     |
//|                                  MT5 to cTrader Copy Trading EA  |
//+------------------------------------------------------------------+
#property copyright "Copyright 2025"
#property version   "1.03"
#property strict

// Input parameters
input string BridgeServerURL   = "http://127.0.0.1:3140";  // Python bridge server URL
input int    RequestTimeout    = 5000;                     // HTTP request timeout in ms
input string MagicNumberFilter = "";                       // Filter by magic number (empty = all trades)
input bool   CopyPendingOrders = true;                     // Copy pending orders (NOT IMPLEMENTED)

// Global variables
struct TradeInfo {
   long   ticket;
   string symbol;
   int    type;
   double volume;
   double openPrice;
   double stopLoss;
   double takeProfit;
   long   magicNumber;
};

TradeInfo g_lastTrades[];
int       g_lastTradeCount = 0;

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
   Print("MT5 CopyTrader EA initialized. Bridge server: ", BridgeServerURL);
   UpdateTradeList();
   Print("Initial positions tracked: ", g_lastTradeCount);
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
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
{
   CheckTradeChanges();
}

//+------------------------------------------------------------------+
//| Update current trade list (dense indexing)                       |
//+------------------------------------------------------------------+
void UpdateTradeList()
{
   int totalPositions = PositionsTotal();
   ArrayResize(g_lastTrades, totalPositions);

   int idx = 0;
   for(int i = 0; i < totalPositions; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      long magic = PositionGetInteger(POSITION_MAGIC);

      // Filter by magic number if specified
      if(MagicNumberFilter != "" && magic != StringToInteger(MagicNumberFilter))
         continue;

      string sym = PositionGetString(POSITION_SYMBOL);

      g_lastTrades[idx].ticket      = (long)ticket;
      g_lastTrades[idx].symbol      = sym;
      g_lastTrades[idx].type        = (int)PositionGetInteger(POSITION_TYPE);
      g_lastTrades[idx].volume      = PositionGetDouble(POSITION_VOLUME);
      g_lastTrades[idx].openPrice   = PositionGetDouble(POSITION_PRICE_OPEN);
      g_lastTrades[idx].stopLoss    = PositionGetDouble(POSITION_SL);
      g_lastTrades[idx].takeProfit  = PositionGetDouble(POSITION_TP);
      g_lastTrades[idx].magicNumber = magic;

      idx++;
   }

   g_lastTradeCount = idx;
   ArrayResize(g_lastTrades, g_lastTradeCount);
}

//+------------------------------------------------------------------+
//| Check for trade changes and send to bridge                       |
//+------------------------------------------------------------------+
void CheckTradeChanges()
{
   int currentPositions = PositionsTotal();

   // New positions / modifications / partial closes
   for(int i = 0; i < currentPositions; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;

      string symbol = PositionGetString(POSITION_SYMBOL);
      long   magic  = PositionGetInteger(POSITION_MAGIC);

      // Filter by magic number if specified
      if(MagicNumberFilter != "" && magic != StringToInteger(MagicNumberFilter))
         continue;

      double currentVol = PositionGetDouble(POSITION_VOLUME);
      double currentSL  = PositionGetDouble(POSITION_SL);
      double currentTP  = PositionGetDouble(POSITION_TP);

      bool isNew = true;
      for(int j = 0; j < g_lastTradeCount; j++)
      {
         if(g_lastTrades[j].ticket == (long)ticket)
         {
            isNew = false;

            // Detect partial close: volume reduced while ticket still exists
            if(currentVol < g_lastTrades[j].volume)
            {
               double closedPart = g_lastTrades[j].volume - currentVol;
               PrintFormat("Partial close detected: ticket=%I64u symbol=%s oldVol=%.2f newVol=%.2f closedPart=%.2f",
                           ticket, symbol, g_lastTrades[j].volume, currentVol, closedPart);

               // Send CLOSE with remaining volume (currentVol)
               SendCloseSignal((long)ticket, symbol, currentVol);
               g_lastTrades[j].volume = currentVol;
            }

            // Detect SL/TP modification
            if(currentSL != g_lastTrades[j].stopLoss || currentTP != g_lastTrades[j].takeProfit)
            {
               SendModifySignal(ticket, currentSL, currentTP);
               g_lastTrades[j].stopLoss   = currentSL;
               g_lastTrades[j].takeProfit = currentTP;
            }
            break;
         }
      }

      if(isNew)
      {
         SendOpenSignal(ticket);
      }
   }

   // Detect fully closed positions (ticket disappeared)
   for(int i = 0; i < g_lastTradeCount; i++)
   {
      bool exists = false;
      for(int j = 0; j < currentPositions; j++)
      {
         ulong ticket = PositionGetTicket(j);
         if((long)ticket == g_lastTrades[i].ticket)
         {
           exists = true;
           break;
         }
      }

      if(!exists)
      {
         // Use last known symbol & volume from g_lastTrades
         long   ticket  = g_lastTrades[i].ticket;
         string symbol  = g_lastTrades[i].symbol;
         double volume  = g_lastTrades[i].volume;

         PrintFormat("Full close detected: ticket=%I64d symbol=%s lastVol=%.2f",
                     ticket, symbol, volume);

         SendCloseSignal(ticket, symbol, volume);
      }
   }

   UpdateTradeList();
}

//+------------------------------------------------------------------+
//| Send open trade signal to bridge server                          |
//+------------------------------------------------------------------+
void SendOpenSignal(ulong ticket)
{
   if(!PositionSelectByTicket(ticket))
   {
      Print("SendOpenSignal: PositionSelectByTicket failed for ", ticket);
      return;
   }

   string symbol    = PositionGetString(POSITION_SYMBOL);
   int    type      = (int)PositionGetInteger(POSITION_TYPE);
   double volume    = PositionGetDouble(POSITION_VOLUME);
   double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
   double sl        = PositionGetDouble(POSITION_SL);
   double tp        = PositionGetDouble(POSITION_TP);
   long   magic     = PositionGetInteger(POSITION_MAGIC);

   double contract_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   double vol_min       = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double vol_max       = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double vol_step      = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);

   string tradeType = (type == POSITION_TYPE_BUY) ? "BUY" : "SELL";

   string jsonData = "{"
      "\"action\":\"OPEN\","
      "\"ticket\":" + (string)ticket + ","
      "\"symbol\":\"" + symbol + "\","
      "\"type\":\"" + tradeType + "\","
      "\"volume\":" + DoubleToString(volume, 2) + ","
      "\"price\":" + DoubleToString(openPrice, 5) + ","
      "\"sl\":" + DoubleToString(sl, 5) + ","
      "\"tp\":" + DoubleToString(tp, 5) + ","
      "\"magic\":" + (string)magic + ","
      "\"mt5_contract_size\":" + DoubleToString(contract_size, 2) + ","
      "\"mt5_volume_min\":" + DoubleToString(vol_min, 2) + ","
      "\"mt5_volume_max\":" + DoubleToString(vol_max, 2) + ","
      "\"mt5_volume_step\":" + DoubleToString(vol_step, 2) +
      "}";

   SendToServer(jsonData);
   Print("Sent OPEN signal for ticket #", ticket, ": ", symbol, " ", tradeType, " ", volume);
}

//+------------------------------------------------------------------+
//| Send close trade signal to bridge server (full or partial)       |
//+------------------------------------------------------------------+
void SendCloseSignal(long ticket, string symbol, double remainingVolume)
{
   string jsonData = "{"
      "\"action\":\"CLOSE\","
      "\"ticket\":" + (string)ticket + ",";

   if(symbol != "")
      jsonData += "\"symbol\":\"" + symbol + "\",";

   jsonData +=
      "\"volume\":" + DoubleToString(remainingVolume, 2) +
      "}";

   SendToServer(jsonData);
   Print("Sent CLOSE signal for ticket #", ticket,
         " symbol=", symbol, " remainingVolume=", remainingVolume);
}

//+------------------------------------------------------------------+
//| Send modify trade signal to bridge server                        |
//+------------------------------------------------------------------+
void SendModifySignal(ulong ticket, double sl, double tp)
{
   string symbol = "";
   for(int i = 0; i < g_lastTradeCount; i++)
   {
      if(g_lastTrades[i].ticket == (long)ticket)
      {
         symbol = g_lastTrades[i].symbol;
         break;
      }
   }

   string jsonData = "{"
      "\"action\":\"MODIFY\","
      "\"ticket\":" + (string)ticket + ",";

   if(symbol != "")
      jsonData += "\"symbol\":\"" + symbol + "\",";

   jsonData +=
      "\"sl\":" + DoubleToString(sl, 5) + ","
      "\"tp\":" + DoubleToString(tp, 5) +
      "}";

   SendToServer(jsonData);
   Print("Sent MODIFY signal for ticket #", ticket, ": ", symbol, " SL=", sl, " TP=", tp);
}

//+------------------------------------------------------------------+
//| Send HTTP POST request to bridge server                          |
//+------------------------------------------------------------------+
void SendToServer(string jsonData)
{
   char   post[];
   char   result[];
   string headers;

   Print("DEBUG JSON -> ", jsonData);

   StringToCharArray(jsonData, post, 0, StringLen(jsonData));

   string url = BridgeServerURL + "/trade_signal";
   headers    = "Content-Type: application/json\r\n";

   ResetLastError();
   int res = WebRequest(
      "POST",
      url,
      headers,
      RequestTimeout,
      post,
      result,
      headers
   );

   if(res == -1)
   {
      int error = GetLastError();
      Print("WebRequest error: ", error,
            ". Make sure URL is added to allowed URLs in Tools > Options > Expert Advisors");
      return;
   }

   if(res == 200)
      Print("Signal sent successfully to bridge server");
   else
      Print("Bridge server returned status code: ", res);
}
//+------------------------------------------------------------------+
