#ifndef COPYTRADER_SIGNALS_MQH
#define COPYTRADER_SIGNALS_MQH

bool GetSymbolTradeMeta(const string symbol,
                        double &contract_size,
                        double &vol_min,
                        double &vol_max,
                        double &vol_step)
{
   contract_size = MarketInfo(symbol, MODE_LOTSIZE);
   vol_min       = MarketInfo(symbol, MODE_MINLOT);
   vol_max       = MarketInfo(symbol, MODE_MAXLOT);
   vol_step      = MarketInfo(symbol, MODE_LOTSTEP);

   if(contract_size <= 0.0)
   {
      Print("GetSymbolTradeMeta: invalid contract_size for ", symbol, " = ", contract_size);
      return false;
   }

   return true;
}

void SendOpenSignal(int ticket)
{
   if(!OrderSelect(ticket, SELECT_BY_TICKET))
   {
      Print("SendOpenSignal: OrderSelect failed for ", ticket);
      return;
   }

   string symbol    = OrderSymbol();
   int    type      = OrderType();
   double volume    = OrderLots();
   double openPrice = OrderOpenPrice();
   double sl        = OrderStopLoss();
   double tp        = OrderTakeProfit();
   int    magic     = OrderMagicNumber();

   double contract_size, vol_min, vol_max, vol_step;
   GetSymbolTradeMeta(symbol, contract_size, vol_min, vol_max, vol_step);

   string tradeType = (type == OP_BUY) ? "BUY" : "SELL";

   string jsonData = "{"
      "\"action\":\"OPEN\","
      "\"ticket\":" + IntegerToString(ticket) + ","
      "\"symbol\":\"" + JsonEscape(symbol) + "\","
      "\"type\":\"" + tradeType + "\","
      "\"volume\":" + DoubleToString(volume, 2) + ","
      "\"price\":" + DoubleToString(openPrice, Digits) + ","
      "\"entry_price\":" + DoubleToString(openPrice, Digits) + ","
      "\"sl\":" + DoubleToString(sl, Digits) + ","
      "\"tp\":" + DoubleToString(tp, Digits) + ","
      "\"magic\":" + IntegerToString(magic) + ","
      "\"mt5_contract_size\":" + DoubleToString(contract_size, 2) + ","
      "\"mt5_volume_min\":" + DoubleToString(vol_min, 2) + ","
      "\"mt5_volume_max\":" + DoubleToString(vol_max, 2) + ","
      "\"mt5_volume_step\":" + DoubleToString(vol_step, 2) +
   "}";

   SendToServer(jsonData);
   Print("Sent OPEN signal for ticket #", ticket, ": ", symbol, " ", tradeType, " ", volume);
}

void SendCloseSignal(long ticket, string symbol, double closedVolume)
{
   double contract_size = 0.0, vol_min = 0.0, vol_max = 0.0, vol_step = 0.0;

   if(symbol != "")
      GetSymbolTradeMeta(symbol, contract_size, vol_min, vol_max, vol_step);

   string jsonData = "{"
      "\"action\":\"CLOSE\","
      "\"ticket\":" + IntegerToString((int)ticket) + ",";

   if(symbol != "")
      jsonData += "\"symbol\":\"" + JsonEscape(symbol) + "\",";

   jsonData += "\"volume\":" + DoubleToString(closedVolume, 8);

   if(symbol != "" && contract_size > 0.0)
   {
      jsonData += ",\"mt5_contract_size\":" + DoubleToString(contract_size, 2);
      jsonData += ",\"mt5_volume_min\":" + DoubleToString(vol_min, 2);
      jsonData += ",\"mt5_volume_max\":" + DoubleToString(vol_max, 2);
      jsonData += ",\"mt5_volume_step\":" + DoubleToString(vol_step, 2);
   }

   jsonData += "}";

   SendToServer(jsonData);
   Print("Sent CLOSE signal for ticket #", ticket,
         " symbol=", symbol, " closedVolume=", closedVolume);
}

void SendModifySignal(int ticket, double sl, double tp)
{
   string symbol = "";

   for(int i = 0; i < g_lastTradeCount; i++)
   {
      if(g_lastTrades[i].ticket == ticket)
      {
         symbol = g_lastTrades[i].symbol;
         break;
      }
   }

   int priceDigits = Digits;
   if(symbol != "")
      priceDigits = (int)MarketInfo(symbol, MODE_DIGITS);

   string jsonData = "{"
      "\"action\":\"MODIFY\","
      "\"ticket\":" + IntegerToString(ticket) + ",";

   if(symbol != "")
      jsonData += "\"symbol\":\"" + JsonEscape(symbol) + "\",";

   jsonData +=
      "\"sl\":" + DoubleToString(sl, priceDigits) + ","
      "\"tp\":" + DoubleToString(tp, priceDigits) +
   "}";

   SendToServer(jsonData);
   Print("Sent MODIFY signal for ticket #", ticket, ": ", symbol, " SL=", sl, " TP=", tp);
}

#endif // COPYTRADER_SIGNALS_MQH
