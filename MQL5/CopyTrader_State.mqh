#ifndef COPYTRADER_STATE_MQH
#define COPYTRADER_STATE_MQH

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

#endif // COPYTRADER_STATE_MQH
