// ASS 2 - hamming code

#include <stdio.h>
#include <math.h>

int main() {
    int data[20], hamming[30];
    int m, r = 0;

    printf("Enter number of data bits: ");
    scanf("%d", &m);

    printf("Enter data bits:\n");
    for (int i = 1; i <= m; i++)
        scanf("%d", &data[i]);

    while ((pow(2, r)) < (m + r + 1))
        r++;

    int total = m + r;

    int j = 1;
    for (int i = 1; i <= total; i++) {
        if ((i & (i - 1)) == 0)  
            hamming[i] = 0;
        else
            hamming[i] = data[j++];
    }

    for (int i = 0; i < r; i++) {
        int pos = pow(2, i);
        int parity = 0;

        for (int j = 1; j <= total; j++) {
            if (j & pos)
                parity ^= hamming[j];
        }

        hamming[pos] = parity;
    }

    printf("\nTransmitted Hamming Code:\n");
    for (int i = total; i >= 1; i--)
        printf("%d ", hamming[i]);

    printf("\n");
    
    int received[30];

    printf("\nEnter the received Hamming code (position 1 to %d):\n", total);
    for (int i = 1; i <= total; i++)
    {
        scanf("%d", &received[i]);
    }
    
    int error = 0;
    
    for (int i = 0; i < r; i++)
    {
        int pos = (int)pow(2, i);
        int parity = 0;
    
        for (int j = 1; j <= total; j++)
        {
            if (j & pos)
                parity ^= received[j];
        }
    
        if (parity)
            error += pos;
    }
    
    if (error == 0)
    {
        printf("\nNo error detected.\n");
    }
    else
    {
        printf("\nError detected at position %d.\n", error);
    
        received[error] ^= 1;
    
        printf("Corrected Hamming Code:\n");
        for (int i = total; i >= 1; i--)
            printf("%d ", hamming[i]);
    
        printf("\n");
    }
    
    return 0;
}