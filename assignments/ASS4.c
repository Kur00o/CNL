#include <stdio.h>
// 0 2 999 1
// 2 0 3 999
// 999 3 0 4
// 1 999 4 0
#define N 4
#define INF 999

int main()
{
    int cost[N][N];
    int dist[N][N];
    int nextHop[N][N];

    int i, j, k;
    int updated;

    printf("Enter the cost matrix:\n");
    printf("(Enter %d for INF)\n\n", INF);

    for (i = 0; i < N; i++)
    {
        for (j = 0; j < N; j++)
        {
            scanf("%d", &cost[i][j]);
        }
    }

    for (i = 0; i < N; i++)
    {
        for (j = 0; j < N; j++)
        {
            dist[i][j] = cost[i][j];

            if (i == j)
            {
                nextHop[i][j] = i;
            }
            else if (cost[i][j] != INF)
            {
                nextHop[i][j] = j;
            }
            else
            {
                nextHop[i][j] = -1;
            }
        }
    }

    do
    {
        updated = 0;

        for (i = 0; i < N; i++)
        {
            for (j = 0; j < N; j++)
            {
                for (k = 0; k < N; k++)
                {
                    if (dist[i][k] != INF &&
                        dist[k][j] != INF &&
                        dist[i][j] > dist[i][k] + dist[k][j])
                    {
                        dist[i][j] = dist[i][k] + dist[k][j];

                        nextHop[i][j] = nextHop[i][k];

                        updated = 1;
                    }
                }
            }
        }

    } while (updated);

    printf("\n========== FINAL ROUTING TABLES ==========\n");

    for (i = 0; i < N; i++)
    {
        printf("\nRouting Table for Router %d\n", i + 1);
        printf("--------------------------------\n");
        printf("Destination\tCost\tNext Hop\n");

        for (j = 0; j < N; j++)
        {
            printf("%d\t\t\t", j + 1);

            if (dist[i][j] == INF)
                printf("INF\t\t");
            else
                printf("%d\t\t", dist[i][j]);

            if (nextHop[i][j] == -1)
                printf("-\n");
            else
                printf("%dR\n", nextHop[i][j] + 1);
        }
    }

    return 0;
}