#ifndef COPYTRADER_STATE_MQH
#define COPYTRADER_STATE_MQH

// -----------------------------
// Open trades state
// -----------------------------
struct TradeInfo
{
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
int g_lastTradeCount = 0;

// -----------------------------
// Pending orders state
// -----------------------------
struct PendingInfo
{
   long     ticket;
   string   symbol;
   int      type;
   double   volume;
   double   priceOpen;
   double   priceStopLimit;
   double   stopLoss;
   double   takeProfit;
   long     magicNumber;
   datetime expiration;
};

PendingInfo g_lastPendings[];
int g_lastPendingCount = 0;

// Track which pending tickets were already sent
long g_sentPendingTickets[];
int  g_sentPendingCount = 0;

#endif // COPYTRADER_STATE_MQH
