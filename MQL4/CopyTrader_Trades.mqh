#ifndef COPYTRADER_TRADES_MQH
#define COPYTRADER_TRADES_MQH

bool IsMarketOrderType(int type)
{
   return (type == OP_BUY || type == OP_SELL);
}

bool IsTrackedTradeTicket(int ticket)
{
   for(int i = 0; i < g_lastTradeCount; i++)
   {
      if(g_lastTrades[i].ticket == ticket)
         return true;
   }
   return false;
}

void UpdateTradeList()
{
   int totalOrders = OrdersTotal();
   ArrayResize(g_lastTrades, totalOrders);

   int idx = 0;

   for(int i = 0; i < totalOrders; i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;

      int type = OrderType();
      if(!IsMarketOrderType(type))
         continue;

      long magic = OrderMagicNumber();
      if(MagicNumberFilter != "" && magic != StringToInteger(MagicNumberFilter))
         continue;

      g_lastTrades[idx].ticket      = OrderTicket();
      g_lastTrades[idx].symbol      = OrderSymbol();
      g_lastTrades[idx].type        = type;
      g_lastTrades[idx].volume      = OrderLots();
      g_lastTrades[idx].openPrice   = OrderOpenPrice();
      g_lastTrades[idx].stopLoss    = OrderStopLoss();
      g_lastTrades[idx].takeProfit  = OrderTakeProfit();
      g_lastTrades[idx].magicNumber = magic;

      idx++;
   }

   g_lastTradeCount = idx;
   ArrayResize(g_lastTrades, g_lastTradeCount);
}

void StartupSyncOpenTrades()
{
   int totalOrders = OrdersTotal();
   int syncedCount = 0;

   for(int i = 0; i < totalOrders; i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;

      int type = OrderType();
      if(!IsMarketOrderType(type))
         continue;

      int ticket = OrderTicket();
      long magic = OrderMagicNumber();

      if(MagicNumberFilter != "" && magic != StringToInteger(MagicNumberFilter))
         continue;

      if(IsTrackedTradeTicket(ticket))
         continue;

      PrintFormat("Startup market sync: sending OPEN for existing ticket=%d symbol=%s lots=%.2f openPrice=%.5f magic=%d",
                  ticket, OrderSymbol(), OrderLots(), OrderOpenPrice(), (int)magic);

      // Requires CopyTrader_Signals.mqh to support:
      // void SendOpenSignal(int ticket, bool startupSync=false)
      SendOpenSignal(ticket, true);
      syncedCount++;
   }

   PrintFormat("Startup market sync complete: syncedCount=%d", syncedCount);

   // Refresh baseline after sending startup sync events
   UpdateTradeList();
}

void CheckTradeChanges()
{
   int totalOrders = OrdersTotal();

   for(int i = 0; i < totalOrders; i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;

      int type = OrderType();
      if(!IsMarketOrderType(type))
         continue;

      int ticket    = OrderTicket();
      string symbol = OrderSymbol();
      long magic    = OrderMagicNumber();

      if(MagicNumberFilter != "" && magic != StringToInteger(MagicNumberFilter))
         continue;

      double currentVol = OrderLots();
      double currentSL  = OrderStopLoss();
      double currentTP  = OrderTakeProfit();

      bool isNew = true;

      for(int j = 0; j < g_lastTradeCount; j++)
      {
         if(g_lastTrades[j].ticket == ticket)
         {
            isNew = false;

            if(currentVol < g_lastTrades[j].volume)
            {
               double closedPart = g_lastTrades[j].volume - currentVol;

               PrintFormat("Partial close detected: ticket=%d symbol=%s oldVol=%.2f newVol=%.2f closedPart=%.2f magic=%d",
                           ticket, symbol, g_lastTrades[j].volume, currentVol, closedPart, (int)g_lastTrades[j].magicNumber);

               SendCloseSignal(ticket, symbol, closedPart, g_lastTrades[j].magicNumber);
               g_lastTrades[j].volume = currentVol;
            }

            if(currentSL != g_lastTrades[j].stopLoss || currentTP != g_lastTrades[j].takeProfit)
            {
               SendModifySignal(ticket, currentSL, currentTP, g_lastTrades[j].magicNumber);
               g_lastTrades[j].stopLoss = currentSL;
               g_lastTrades[j].takeProfit = currentTP;
            }

            break;
         }
      }

      if(isNew)
      {
         PrintFormat("New market trade detected: ticket=%d symbol=%s lots=%.2f openPrice=%.5f magic=%d",
                     ticket, symbol, currentVol, OrderOpenPrice(), (int)magic);

         // Normal live open, not startup recovery
         SendOpenSignal(ticket, false);
      }
   }

   for(int i = 0; i < g_lastTradeCount; i++)
   {
      bool exists = false;

      for(int j = 0; j < totalOrders; j++)
      {
         if(!OrderSelect(j, SELECT_BY_POS, MODE_TRADES))
            continue;

         int type = OrderType();
         if(!IsMarketOrderType(type))
            continue;

         long magic = OrderMagicNumber();
         if(MagicNumberFilter != "" && magic != StringToInteger(MagicNumberFilter))
            continue;

         if(OrderTicket() == g_lastTrades[i].ticket)
         {
            exists = true;
            break;
         }
      }

      if(!exists)
      {
         long ticket   = g_lastTrades[i].ticket;
         string symbol = g_lastTrades[i].symbol;
         double volume = g_lastTrades[i].volume;
         long magic    = g_lastTrades[i].magicNumber;

         PrintFormat("Full close detected: ticket=%d symbol=%s lastVol=%.2f magic=%d",
                     (int)ticket, symbol, volume, (int)magic);

         SendCloseSignal(ticket, symbol, volume, magic);
      }
   }

   UpdateTradeList();
}

#endif // COPYTRADER_TRADES_MQH
