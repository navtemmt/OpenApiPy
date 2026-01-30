//+------------------------------------------------------------------+
//|                                           MT5_CopyTrader.mq5     |
//|                                  MT5 to cTrader Copy Trading EA  |
//+------------------------------------------------------------------+
#property copyright "Copyright 2025"
#property version   "1.01"
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

   // Load initial positions
   UpdateTradeList();

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
   // Check for trade changes on every tick
   CheckTradeChanges();
}

//+------------------------------------------------------------------+
//| Update current trade list                                        |
//+------------------------------------------------------------------+
void UpdateTradeList()
{
   int totalPositions = PositionsTotal();
   ArrayResize(g_lastTrades, totalPositions);

   for(int i = 0; i < totalPositions; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0)
      {
         long magic = PositionGetInteger(POSITION_MAGIC);

         // Filter by magic number if specified
         if(MagicNumberFilter != "" && magic != StringToInteger(MagicNumberFilter))
            continue;

         g_lastTrades[i].ticket      = (long)ticket;
         g_lastTrades[i].symbol      = PositionGetString(POSITION_SYMBOL);
         g_lastTrades[i].type        = (int)PositionGetInteger(POSITION_TYPE);
         g_lastTrades[i].volume      = PositionGetDouble(POSITION_VOLUME);
         g_lastTrades[i].openPrice   = PositionGetDouble(POSITION_PRICE_OPEN);
         g_lastTrades[i].stopLoss    = PositionGetDouble(POSITION_SL);
         g_lastTrades[i].takeProfit  = PositionGetDouble(POSITION_TP);
         g_lastTrades[i].magicNumber = magic;
      }
   }

   g_lastTradeCount = totalPositions;
}

//+------------------------------------------------------------------+
//| Check for trade changes and send to bridge                       |
//+------------------------------------------------------------------+
void CheckTradeChanges()
{
   int currentPositions = PositionsTotal();

   // Check for new positions
   for(int i = 0; i < currentPositions; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0)
      {
         long magic = PositionGetInteger(POSITION_MAGIC);

         // Filter by magic number if specified
         if(MagicNumberFilter != "" && magic != StringToInteger(MagicNumberFilter))
            continue;

         // Check if this is a new trade
         bool isNew = true;
         for(int j = 0; j < g_lastTradeCount; j++)
         {
            if(g_lastTrades[j].ticket == (long)ticket)
            {
               isNew = false;

               // Check for modifications
               double currentSL = PositionGetDouble(POSITION_SL);
               double currentTP = PositionGetDouble(POSITION_TP);

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
   }

   // Check for closed positions
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
         SendCloseSignal(g_lastTrades[i].ticket);
      }
   }

   // Update the trade list
   UpdateTradeList();
}

//+------------------------------------------------------------------+
//| Send open trade signal to bridge server                          |
//+------------------------------------------------------------------+
void SendOpenSignal(ulong ticket)
{
   if(!PositionSelectByTicket(ticket))
      return;

   string symbol    = PositionGetString(POSITION_SYMBOL);
   int    type      = (int)PositionGetInteger(POSITION_TYPE);
   double volume    = PositionGetDouble(POSITION_VOLUME);
   double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
   double sl        = PositionGetDouble(POSITION_SL);
   double tp        = PositionGetDouble(POSITION_TP);
   long   magic     = PositionGetInteger(POSITION_MAGIC);

   // --- MT5 symbol properties for better volume mapping ---
   double contract_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   double vol_min       = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double vol_max       = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double vol_step      = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);

   string tradeType = (type == POSITION_TYPE_BUY) ? "BUY" : "SELL";

   string jsonData = StringFormat(
      "{"
         "\\\"action\\\":\\\"OPEN\\\","
         "\\\"ticket\\\":%d,"
         "\\\"symbol\\\":\\\"%s\\\","
         "\\\"type\\\":\\\"%s\\\","
         "\\\"volume\\\":%.2f,"
         "\\\"price\\\":%.5f,"
         "\\\"sl\\\":%.5f,"
         "\\\"tp\\\":%.5f,"
         "\\\"magic\\\":%d,"
         "\\\"mt5_contract_size\\\":%.2f,"
         "\\\"mt5_volume_min\\\":%.2f,"
         "\\\"mt5_volume_max\\\":%.2f,"
         "\\\"mt5_volume_step\\\":%.2f"
      "}",
      ticket,
      symbol,
      tradeType,
      volume,
      openPrice,
      sl,
      tp,
      magic,
      contract_size,
      vol_min,
      vol_max,
      vol_step
   );

   SendToServer(jsonData);
   Print("Sent OPEN signal for ticket #", ticket, ": ", symbol, " ", tradeType, " ", volume);
}

//+------------------------------------------------------------------+
//| Send close trade signal to bridge server                         |
//+------------------------------------------------------------------+
void SendCloseSignal(long ticket)
{
   string jsonData = StringFormat(
      "{\\\"action\\\":\\\"CLOSE\\\",\\\"ticket\\\":%d}",
      ticket
   );

   SendToServer(jsonData);
   Print("Sent CLOSE signal for ticket #", ticket);
}

//+------------------------------------------------------------------+
//| Send modify trade signal to bridge server                        |
//+------------------------------------------------------------------+
void SendModifySignal(ulong ticket, double sl, double tp)
{
   // Look up symbol for this ticket from our last known trades
   string symbol = "";
   for(int i = 0; i < g_lastTradeCount; i++)
   {
      if(g_lastTrades[i].ticket == (long)ticket)
      {
         symbol = g_lastTrades[i].symbol;
         break;
      }
   }

   string jsonData;
   if(symbol != "")
   {
      jsonData = StringFormat(
         "{\\\"action\\\":\\\"MODIFY\\\","
         "\\\"ticket\\\":%d,"
         "\\\"symbol\\\":\\\"%s\\\","
         "\\\"sl\\\":%.5f,"
         "\\\"tp\\\":%.5f}",
         ticket,
         symbol,
         sl,
         tp
      );
   }
   else
   {
      // Fallback: send without symbol if not found
      jsonData = StringFormat(
         "{\\\"action\\\":\\\"MODIFY\\\",\\\"ticket\\\":%d,\\\"sl\\\":%.5f,\\\"tp\\\":%.5f}",
         ticket,
         sl,
         tp
      );
   }

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
